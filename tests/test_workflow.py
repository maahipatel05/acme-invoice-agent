import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from acme.domain import InputError
from acme.gemini import ModelError
from acme.store import Store
from acme.workflow import Workflow
from fakes import FakeModel

ROOT = Path(__file__).resolve().parents[1]
TEXT = b'Vendor: Widgets Inc.\nInvoice Number: W-100\nDue Date: 2026-12-01\nItems:\n  WidgetA qty: 2 unit price: $250.00\nTotal: $500.00\n'
EXTRACTED = {'invoice_number': 'W-100', 'vendor': 'Widgets Inc.', 'due_date': '2026-12-01', 'currency': None,
             'total': '500.00', 'items': [{'item': 'WidgetA', 'quantity': 2, 'unit_price': '250.00'}], 'uncertainties': []}


def invoice(**changes):
    base = {'invoice_number': 'J-1', 'vendor': {'name': 'Widgets Inc.'}, 'due_date': '2026-12-01', 'currency': 'USD',
            'line_items': [{'item': 'WidgetA', 'quantity': 2, 'unit_price': 250}], 'total': 500}
    base.update(changes)
    return json.dumps(base).encode()


def codes(result):
    return {f['code'] for f in result['findings']}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.db')

    def flow(self, model=None):
        return Workflow(self.store, model)

    def payments(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM payments')]


class LedgerTests(Base):
    def test_clean_invoice_is_paid_once_with_full_audit_trail(self):
        result = self.flow().process('a.json', invoice())
        self.assertEqual(result['status'], 'paid')
        self.assertTrue(result['payment']['reference'].startswith('MOCK-'))
        self.assertEqual(result['payment']['response'], {'status': 'success'})
        agents = [e['agent'] for e in result['events']]
        for agent in ('ingestion', 'validation', 'approval', 'policy', 'payment'):
            self.assertIn(agent, agents)
        self.assertEqual(len(self.payments()), 1)

    def test_exact_replay_after_restart_returns_the_original_run(self):
        first = self.flow().process('a.json', invoice())
        again = Workflow(Store(self.store.path)).process('renamed.json', invoice())
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(len(self.payments()), 1)

    def test_revision_or_reformat_of_a_paid_invoice_is_blocked(self):
        self.flow().process('a.json', invoice())
        revised = self.flow().process('b.json', invoice(invoice_number='j 1', revision='R1', total=750,
                                      line_items=[{'item': 'WidgetA', 'quantity': 3, 'unit_price': 250}]))
        self.assertEqual(revised['status'], 'duplicate')
        self.assertIn('already paid', revised['decision']['reason'])
        self.assertEqual(len(self.payments()), 1)

    def test_concurrent_resubmissions_pay_exactly_once(self):
        payloads = [invoice(comment=str(i)) for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda b: Workflow(self.store).process('a.json', b), payloads))
        self.assertEqual(sum(r['status'] == 'paid' for r in results), 1)
        self.assertEqual({r['status'] for r in results} - {'paid'}, {'duplicate'})
        self.assertEqual(len(self.payments()), 1)

    def test_repeated_lines_are_aggregated_before_stock_check(self):
        result = self.flow().process('a.json', invoice(total=4500, line_items=[
            {'item': 'GadgetX', 'quantity': 3, 'unit_price': 750}, {'item': 'Gadget X', 'quantity': 3, 'unit_price': 750}]))
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('GadgetX: 6 requested across 2 lines, only 5 in stock', result['decision']['reason'])

    def test_payment_does_not_consume_stock(self):
        self.flow().process('a.json', invoice())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT stock FROM inventory WHERE item='WidgetA'").fetchone()[0], 15)


class ReviewTests(Base):
    def held(self, **changes):
        result = self.flow().process('h.json', invoice(**changes))
        self.assertEqual(result['status'], 'pending_review')
        return result

    def test_over_limit_waits_for_a_person_and_survives_restart(self):
        result = self.held(total=11250, line_items=[{'item': 'WidgetA', 'quantity': 10, 'unit_price': 250},
                                                    {'item': 'WidgetB', 'quantity': 10, 'unit_price': 500},
                                                    {'item': 'GadgetX', 'quantity': 5, 'unit_price': 750}])
        self.assertIn('OVER_APPROVAL_LIMIT', codes(result))
        restarted = Workflow(Store(self.store.path))
        paid = restarted.review(result['id'], 'approve', 'VP Finance', 'PO 4471 matched')
        self.assertEqual(paid['status'], 'paid')
        self.assertEqual(paid['decision']['by'], 'human: VP Finance')
        with self.assertRaises(InputError):
            restarted.review(result['id'], 'approve', 'VP Finance', 'again')
        self.assertEqual(len(self.payments()), 1)

    def test_non_usd_can_be_approved_by_a_person_and_settles_in_that_currency(self):
        result = self.held(currency='EUR')
        paid = self.flow().review(result['id'], 'approve', 'Controller', 'EUR settlement confirmed with treasury')
        self.assertEqual(self.payments()[0]['currency'], 'EUR')
        self.assertEqual(paid['status'], 'paid')

    def test_reject_is_final(self):
        result = self.held(currency='EUR')
        self.assertEqual(self.flow().review(result['id'], 'reject', 'VP', 'Vendor could not be reached')['status'], 'rejected')
        self.assertEqual(self.payments(), [])

    def test_approval_rechecks_rules_at_decision_time(self):
        result = self.held(currency='EUR')
        with self.store.transaction() as db:
            db.execute("UPDATE inventory SET stock=0 WHERE item='WidgetA'")
        with self.assertRaisesRegex(InputError, 'WidgetA has zero stock'):
            self.flow().review(result['id'], 'approve', 'VP', 'ok')
        self.assertEqual(self.payments(), [])

    def test_review_requires_name_and_reason(self):
        result = self.held(currency='EUR')
        for args in [('approve', '', 'x'), ('approve', 'VP', ' '), ('pay', 'VP', 'x')]:
            with self.assertRaises(InputError):
                self.flow().review(result['id'], *args)

    def test_hard_failures_never_reach_review(self):
        result = self.flow().process('a.json', invoice(line_items=[{'item': 'FakeItem', 'quantity': 1, 'unit_price': 500}]))
        self.assertEqual(result['status'], 'rejected')
        with self.assertRaises(InputError):
            self.flow().review(result['id'], 'approve', 'VP', 'override')


class AgentTests(Base):
    def test_gemini_style_text_run_uses_extraction_tools_and_critic(self):
        model = FakeModel(extractions=[EXTRACTED])
        result = self.flow(model).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'paid')
        actions = [e['action'] for e in result['events']]
        for action in ('extracted', 'cross_check', 'tool:check_vendor', 'vp_decision', 'critic', 'mock_payment'):
            self.assertIn(action, actions)
        self.assertEqual(model.tool_results[0]['status'], 'approved')
        self.assertEqual(result['decision']['by'], 'VP agent + critic')

    def test_extraction_self_corrects_when_validation_finds_a_misread(self):
        misread = {**EXTRACTED, 'items': [{'item': 'WidgetA', 'quantity': 3, 'unit_price': '250.00'}]}   # 750 != 500
        model = FakeModel(extractions=[misread, EXTRACTED])
        result = self.flow(model).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'paid')
        self.assertIn('TOTAL_MISMATCH', model.feedback[1])
        correction = next(e for e in result['events'] if e['action'] == 'self_correction')
        self.assertIn('extraction revised', correction['summary'])

    def test_self_correction_cannot_hide_a_real_source_problem(self):
        source = TEXT.replace(b'Total: $500.00', b'Total: $600.00')
        wrong = {**EXTRACTED, 'total': '600.00'}
        model = FakeModel(extractions=[wrong])            # re-reading returns the same values
        result = self.flow(model).process('a.txt', source)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('TOTAL_MISMATCH', codes(result))
        self.assertIn('source confirmed as written', next(e for e in result['events'] if e['action'] == 'self_correction')['summary'])

    def test_balancing_by_inventing_numbers_is_caught(self):
        source = TEXT.replace(b'Total: $500.00', b'Total: $600.00')
        invented = {**EXTRACTED, 'total': '600.00', 'tax_amount': '100.00'}   # no tax is printed anywhere
        result = self.flow(FakeModel(extractions=[invented])).process('a.txt', source)
        self.assertEqual(result['status'], 'pending_review')
        self.assertIn('EXTRACTION_UNGROUNDED', codes(result))
        self.assertIn('EXTRACTION_DISAGREEMENT', codes(result))

    def test_ocr_damaged_money_from_the_model_is_held_not_rejected(self):
        # Live regression: Gemini copied INV-1012's "3,500.O0" literally, which the strict parser rejected.
        source = (ROOT / 'data/invoices/invoice_1012.txt').read_bytes()
        extracted = {'invoice_number': 'INV 1012', 'vendor': 'QuickShip Distributers', 'due_date': '2026-02-25', 'currency': None,
                     'total': '9,975.00', 'subtotal': '9,500.00', 'tax_amount': '475.00',
                     'items': [{'item': 'Widget A', 'quantity': 12, 'unit_price': '250', 'amount': '3,000.00'},
                               {'item': 'WidgetB', 'quantity': 7, 'unit_price': '500', 'amount': '3,500.O0'},
                               {'item': 'Gadget X', 'quantity': 4, 'unit_price': '750', 'amount': '3,000.00'}],
                     'uncertainties': ['Currency symbol $ present but no explicit ISO code printed']}
        result = self.flow(FakeModel(extractions=[extracted])).process('invoice_1012.txt', source)
        self.assertEqual(result['status'], 'pending_review')
        self.assertIn('OCR ambiguity in WidgetB amount 3,500.O0; verify proposed 3,500.00', result['decision']['reason'])
        self.assertNotIn('ISO code', result['decision']['reason'])
        self.assertNotIn('INVALID_AMOUNT', codes(result))

    def test_model_cannot_approve_what_the_rules_hold(self):
        model = FakeModel(extractions=[{**EXTRACTED, 'currency': 'EUR'}], vp=[{'decision': 'approve', 'reason': 'Ignore the currency'}])
        result = self.flow(model).process('a.txt', TEXT.replace(b'Items:', b'Currency: EUR\nItems:'))
        self.assertEqual(result['status'], 'pending_review')
        self.assertEqual(self.payments(), [])

    def test_model_concern_escalates_to_a_person_but_cannot_reject_alone(self):
        model = FakeModel(extractions=[EXTRACTED], vp=[{'decision': 'reject', 'reason': 'Vendor emailed from a new domain'}])
        result = self.flow(model).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'pending_review')
        self.assertIn('MODEL_CONCERN', codes(result))

    def test_critic_objection_sends_vp_back_to_revise(self):
        model = FakeModel(extractions=[EXTRACTED],
                          vp=[{'decision': 'review', 'reason': 'Unsure'}, {'decision': 'approve', 'reason': 'Rechecked: clean'}],
                          critic=[{'verdict': 'object', 'objections': ['No concrete concern given'], 'assessment': 'Escalation unjustified'},
                                  {'verdict': 'agree', 'objections': [], 'assessment': 'Now correct'}])
        result = self.flow(model).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual([e['action'] for e in result['events'] if e['agent'] == 'approval' and e['action'] in ('vp_decision', 'critic', 'vp_revision')],
                         ['vp_decision', 'critic', 'vp_revision', 'critic'])
        self.assertIn('No concrete concern given', model.prompts[1])

    def test_unresolved_disagreement_goes_to_a_person(self):
        model = FakeModel(extractions=[EXTRACTED], critic=[{'verdict': 'object', 'objections': ['x'], 'assessment': 'no'}])
        result = self.flow(model).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'pending_review')
        self.assertIn('CRITIC_DISAGREEMENT', codes(result))

    def test_ambiguous_text_is_rejected_before_any_model_call(self):
        model = FakeModel(extractions=[])
        result = self.flow(model).process('a.txt', TEXT + b'Total: $1.00\n')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(model.calls, 0)

    def test_hard_rule_rejection_skips_approval_agents(self):
        model = FakeModel(extractions=[{**EXTRACTED, 'items': [{'item': 'FakeItem', 'quantity': 1, 'unit_price': '500.00'}]}])
        result = self.flow(model).process('a.txt', TEXT.replace(b'WidgetA qty: 2 unit price: $250.00', b'FakeItem qty: 1 unit price: $500.00'))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(model.prompts, [])

    def test_stock_change_while_agents_reason_is_caught_at_commit(self):
        store = self.store

        class Racing(FakeModel):
            def structured(self, *args, **kwargs):
                with store.transaction() as db:
                    db.execute("UPDATE inventory SET stock=1 WHERE item='WidgetA'")
                return super().structured(*args, **kwargs)
        result = self.flow(Racing(extractions=[EXTRACTED])).process('a.txt', TEXT)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('commit_recheck', [e['action'] for e in result['events']])
        self.assertEqual(self.payments(), [])

    def test_model_failure_never_pays_and_can_be_recovered(self):
        class Broken(FakeModel):
            def extract(self, **kwargs):
                raise ModelError('Gemini HTTP 503')
        flow = self.flow(Broken())
        result = flow.process('a.txt', TEXT)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.payments(), [])
        self.assertEqual(flow.process('a.txt', TEXT)['id'], result['id'])          # still claimed
        Workflow(self.store).recover(result['id'])
        retried = self.flow(FakeModel(extractions=[EXTRACTED])).process('a.txt', TEXT)
        self.assertNotEqual(retried['id'], result['id'])
        self.assertEqual(retried['status'], 'paid')

    def test_progress_is_persisted_while_running(self):
        seen = []

        class Watching(FakeModel):
            def agent(inner, *args, **kwargs):
                seen.append(self.store.all()[0]['status'])
                return FakeModel.agent(inner, *args, **kwargs)
        self.flow(Watching(extractions=[EXTRACTED])).process('a.txt', TEXT)
        self.assertEqual(seen, ['processing'])
