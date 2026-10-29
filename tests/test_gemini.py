"""Gemini adapter contract: retries, quota handling, secrecy and the function-calling loop (no network)."""
import io
import json
import socket
import ssl
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch
from acme.gemini import DEFAULT_MODEL, Gemini, ModelError, declaration


def http_error(code, payload=None, headers=None):
    return urllib.error.HTTPError('redacted', code, 'err', headers, io.BytesIO(json.dumps(payload or {}).encode()))


def ok(parts=None):
    return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': parts or [{'text': 'OK'}]}}]}).encode())


class TransportTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        with patch.dict('os.environ', {}, clear=True):
            model = Gemini(api_key='k')
            self.assertEqual((model.model, model.timeout_seconds), (DEFAULT_MODEL, 90))
            with self.assertRaises(ModelError):
                Gemini()
        self.assertEqual(Gemini(api_key='k', model='models/gemini-3.5-flash').model, 'gemini-3.5-flash')
        for bad in ('NaN', 'inf', '0', '181', 'x'):
            with self.assertRaises(ModelError):
                Gemini(api_key='k', timeout_seconds=bad)

    def test_503_backs_off_and_recovers_then_gives_up_after_five(self):
        model = Gemini(api_key='k')
        with patch('urllib.request.urlopen', side_effect=[http_error(503)] * 3 + [ok()]), \
                patch('time.sleep') as sleep, patch('random.uniform', return_value=0):
            self.assertEqual(model.request({})['parts'][0]['text'], 'OK')
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0, 8.0])
        model = Gemini(api_key='k')
        with patch('urllib.request.urlopen', side_effect=[http_error(503)] * 5), patch('time.sleep'), patch('random.uniform', return_value=0):
            with self.assertRaises(ModelError):
                model.request({})
        self.assertEqual(model.calls, 5)

    def test_retry_after_honoured_and_long_waits_surfaced(self):
        short, long = Message(), Message()
        short['Retry-After'], long['Retry-After'] = '7', '120'
        with patch('urllib.request.urlopen', side_effect=[http_error(503, headers=short), ok()]), \
                patch('time.sleep') as sleep, patch('random.uniform', return_value=0):
            Gemini(api_key='k').request({})
        sleep.assert_called_once_with(7.0)
        with patch('urllib.request.urlopen', side_effect=http_error(503, headers=long)), patch('time.sleep') as sleep:
            with self.assertRaisesRegex(ModelError, '120s'):
                Gemini(api_key='k').request({})
        sleep.assert_not_called()

    def test_per_minute_quota_waits_when_asked_then_succeeds(self):
        payload = {'error': {'status': 'RESOURCE_EXHAUSTED', 'details': [
            {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '45s'}]}}
        with patch('urllib.request.urlopen', side_effect=[http_error(429, payload), ok()]), patch('time.sleep') as sleep:
            Gemini(api_key='k').request({})
        sleep.assert_called_once_with(45.0)
        payload['error']['details'][0]['retryDelay'] = '300s'
        with patch('urllib.request.urlopen', side_effect=http_error(429, payload)), patch('time.sleep') as sleep:
            with self.assertRaisesRegex(ModelError, '300s'):
                Gemini(api_key='k').request({})
        sleep.assert_not_called()

    def test_zero_quota_fails_fast_without_echoing_provider_text(self):
        payload = {'error': {'status': 'RESOURCE_EXHAUSTED', 'message': 'limit: 0. echo secret-key',
                             'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                                          'violations': [{'quotaId': 'PerDay-FreeTier'}]}]}}
        model = Gemini(api_key='secret-key')
        with patch('urllib.request.urlopen', side_effect=http_error(429, payload)), patch('time.sleep') as sleep:
            with self.assertRaises(ModelError) as caught:
                model.request({})
        self.assertIn('limit is 0', str(caught.exception))
        self.assertIn('PerDay-FreeTier', str(caught.exception))
        self.assertNotIn('secret-key', str(caught.exception))
        sleep.assert_not_called()

    def test_auth_404_tls_dns_and_timeouts(self):
        for error, calls in [(http_error(403), 1), (http_error(404, {'error': {'status': 'NOT_FOUND'}}), 1),
                             (urllib.error.URLError(ssl.SSLCertVerificationError('x')), 1),
                             (urllib.error.URLError(socket.gaierror('x')), 3), (TimeoutError('secret-key'), 3)]:
            with self.subTest(error=error):
                model = Gemini(api_key='secret-key')
                with patch('urllib.request.urlopen', side_effect=error), patch('time.sleep'):
                    with self.assertRaises(ModelError) as caught:
                        model.request({})
                self.assertEqual(model.calls, calls)
                self.assertNotIn('secret-key', str(caught.exception))

    def test_malformed_envelopes_and_incomplete_output_reject(self):
        for payload in [{'candidates': {}}, {'candidates': [{'content': {'parts': 'x'}}]}, [],
                        {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': '{'}]}}]}]:
            with self.subTest(payload=payload):
                with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
                    with self.assertRaises(ModelError):
                        Gemini(api_key='k').request({})

    def test_key_is_sent_in_header_not_url_and_models_paginate(self):
        pages = [{'models': [{'name': 'models/a', 'supportedGenerationMethods': ['generateContent']}], 'nextPageToken': 'n/2'},
                 {'models': [{'name': 'models/b', 'supportedGenerationMethods': ['embedContent']}]}]
        seen = []
        with patch('urllib.request.urlopen', side_effect=lambda req, timeout: seen.append(req) or io.BytesIO(json.dumps(pages.pop(0)).encode())):
            self.assertEqual(Gemini(api_key='secret-key').list_models(), ['a'])
        self.assertNotIn('secret-key', seen[0].full_url)
        self.assertIn('pageToken=n%2F2', seen[1].full_url)


class StructuredTests(unittest.TestCase):
    def test_duplicate_keys_and_non_objects_reject(self):
        for text in ['{"decision":"reject","decision":"approve"}', '[]', 'not json']:
            model = Gemini(api_key='k')
            with patch.object(model, 'request', return_value={'parts': [{'text': text}]}):
                with self.assertRaises(ModelError):
                    model.structured('x', [{'text': 'y'}], {'type': 'OBJECT'})

    def test_gemini3_uses_default_sampling_older_models_temperature_zero(self):
        for name, has_temperature in [('gemini-3.5-flash', False), ('gemini-2.5-flash', True)]:
            model = Gemini(api_key='k', model=name)
            with patch.object(model, 'request', return_value={'parts': [{'text': '{}'}]}) as request:
                model.structured('x', [{'text': 'y'}], {'type': 'OBJECT'})
            self.assertEqual('temperature' in request.call_args.args[0]['generationConfig'], has_temperature)


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tools = {'lookup_item': (declaration('lookup_item', 'x', {'name': {'type': 'STRING'}}, ['name']),
                                      lambda name: self.calls.append(name) or {'found': name == 'WidgetA'}),
                      'submit_decision': (declaration('submit_decision', 'done', {'decision': {'type': 'STRING'}}), None)}

    def test_parallel_tool_calls_ids_and_thought_signatures_round_trip(self):
        first = {'role': 'model', 'parts': [
            {'functionCall': {'id': 'c1', 'name': 'lookup_item', 'args': {'name': 'WidgetA'}}, 'thoughtSignature': 'sig'},
            {'functionCall': {'id': 'c2', 'name': 'lookup_item', 'args': {'name': 'WidgetZ'}}}]}
        final = {'role': 'model', 'parts': [{'functionCall': {'name': 'submit_decision', 'args': {'decision': 'approve'}}}]}
        model = Gemini(api_key='k')
        with patch.object(model, 'request', side_effect=[first, final]) as request:
            answer, history = model.agent('sys', 'case', self.tools, 'submit_decision', 'approval.vp')
        self.assertEqual(answer, {'decision': 'approve'})
        self.assertEqual(self.calls, ['WidgetA', 'WidgetZ'])
        second = request.call_args_list[1].args[0]
        self.assertEqual(second['contents'][1], first)                       # signature preserved verbatim
        replies = second['contents'][2]['parts']
        self.assertEqual([r['functionResponse']['id'] for r in replies], ['c1', 'c2'])
        self.assertEqual(replies[1]['functionResponse']['response'], {'found': False})
        self.assertEqual(second['toolConfig']['functionCallingConfig']['mode'], 'ANY')
        self.assertEqual([s for s in (model.request_trace or [])], [])          # request was patched

    def test_unknown_tool_and_bad_arguments_are_reported_back_not_executed(self):
        bad = {'role': 'model', 'parts': [{'functionCall': {'name': 'pay_now', 'args': {}}},
                                          {'functionCall': {'name': 'lookup_item', 'args': {'wrong': 1}}}]}
        final = {'role': 'model', 'parts': [{'functionCall': {'name': 'submit_decision', 'args': {'decision': 'review'}}}]}
        model = Gemini(api_key='k')
        with patch.object(model, 'request', side_effect=[bad, final]) as request:
            model.agent('sys', 'case', self.tools, 'submit_decision', 'approval.vp')
        replies = request.call_args_list[1].args[0]['contents'][2]['parts']
        self.assertIn('Unknown tool', replies[0]['functionResponse']['response']['error'])
        self.assertIn('Invalid arguments', replies[1]['functionResponse']['response']['error'])
        self.assertEqual(self.calls, [])

    def test_turn_budget_and_missing_calls(self):
        loop = {'role': 'model', 'parts': [{'functionCall': {'name': 'lookup_item', 'args': {'name': 'WidgetA'}}}]}
        model = Gemini(api_key='k')
        with patch.object(model, 'request', return_value=loop):
            with self.assertRaisesRegex(ModelError, 'did not finish within 3 turns'):
                model.agent('sys', 'case', self.tools, 'submit_decision', 'vp', max_turns=3)
        with patch.object(model, 'request', return_value={'parts': [{'text': 'I approve'}]}):
            with self.assertRaisesRegex(ModelError, 'no function call'):
                model.agent('sys', 'case', self.tools, 'submit_decision', 'vp')

    def test_history_continues_the_same_conversation(self):
        final = {'role': 'model', 'parts': [{'functionCall': {'name': 'submit_decision', 'args': {'decision': 'approve'}}}]}
        model = Gemini(api_key='k')
        with patch.object(model, 'request', return_value=final) as request:
            _, history = model.agent('sys', 'first', self.tools, 'submit_decision', 'vp')
            model.agent('sys', 'critic objected', self.tools, 'submit_decision', 'vp', history=history)
        texts = [p.get('text') for c in request.call_args.args[0]['contents'] for p in c['parts'] if 'text' in p]
        self.assertEqual(texts, ['first', 'critic objected'])
