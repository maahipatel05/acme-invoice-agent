"""Orchestrator: ingest -> validate -> approve (VP <-> critic) -> policy gate -> pay | reject | hold.

Every step is persisted as it happens, so the console shows agents working live and a
crash leaves an inspectable record. The simulated ledger and workflow state commit in
one SQLite transaction (atomic for this local mock; see README for real-bank boundaries).
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .agents import ApprovalAgents, IngestionAgent, ValidationAgent, combine
from .domain import BLOCK, HOLD, InputError, finding, floor_decision, normalize, summarize, usd
from .gemini import ModelError
from .ingestion import read_document
from .store import Store, mock_payment
from . import tools

POLICY_VERSION = '2026-10-v2'
STATUS = {'approve': 'paid', 'review': 'pending_review', 'reject': 'rejected'}


class Workflow:
    def __init__(self, store, model=None, log=None):
        self.store = store
        self.model = model
        self.log = log

    # ---- intake --------------------------------------------------------------------------
    def start(self, filename, data):
        """Claim a run for these exact bytes. Returns (result, is_new)."""
        digest = hashlib.sha256(data).hexdigest()
        result = {'id': uuid.uuid4().hex, 'filename': Path(filename).name, 'source_hash': digest,
                  'status': 'processing', 'mode': 'gemini' if self.model else 'offline',
                  'model': getattr(self.model, 'model', None), 'policy_version': POLICY_VERSION,
                  'received_at': datetime.now(timezone.utc).isoformat(), 'findings': [], 'events': []}
        with self.store.transaction() as db:
            previous = db.execute('SELECT result FROM runs WHERE source_hash=?', (digest,)).fetchone()
            if previous:
                return json.loads(previous['result']), False
            db.execute('INSERT INTO runs(id, source_hash, filename, status, result) VALUES (?,?,?,?,?)',
                       (result['id'], digest, result['filename'], result['status'], json.dumps(result)))
        return result, True

    def process(self, filename, data):
        result, new = self.start(filename, data)
        return self.run(result, filename, data) if new else result

    # ---- the pipeline --------------------------------------------------------------------
    def run(self, result, filename, data):
        started = time.monotonic()

        def record(agent, action, summary, detail=None):
            entry = {'at': datetime.now(timezone.utc).isoformat(), 'ms': round((time.monotonic() - started) * 1000),
                     'agent': agent, 'action': action, 'summary': summary, 'detail': detail}
            result['events'].append(entry)
            if self.log:
                self.log({k: entry[k] for k in ('ms', 'agent', 'action', 'summary')})

        def emit(agent, action, summary, detail=None):
            record(agent, action, summary, detail)
            self.store.checkpoint(result)

        try:
            document = read_document(filename, data, allow_scanned=self.model is not None)
            raw, extraction_findings = IngestionAgent(self.model).run(document, data, emit)
            invoice, findings = normalize(raw)
            findings += extraction_findings
            result['invoice'] = invoice
            result['identity'] = tools.identity(invoice['vendor'], invoice['invoice_number'])
            with self.store.connect() as db:
                findings += ValidationAgent().run(db, invoice, document['text'], result['id'])
            result['findings'] = findings
            floor = floor_decision(findings)
            emit('validation', 'checked', self._finding_summary(findings, floor),
                 {'floor': floor, 'codes': [f['code'] for f in findings]})

            if floor == 'reject':
                advice = {'decision': 'reject', 'reason': summarize(findings, (BLOCK,)), 'by': 'hard rules'}
                emit('approval', 'skipped', 'Hard-rule rejection; no agent or reviewer can override it', None)
            else:
                advice = ApprovalAgents(self.model).run(self.store, invoice, findings, emit)
            decision, extra = combine(floor, advice)
            findings += extra
            result['advice'] = advice
            emit('policy', 'gate', f'Deterministic minimum {floor}; agents advised {advice["decision"]}; final {decision}',
                 {'floor': floor, 'advice': advice['decision'], 'final': decision})
            self._settle(result, decision, advice, record)  # settles inside one transaction; no checkpoints
        except InputError as exc:
            result['findings'] = [finding('UNREADABLE_DOCUMENT', BLOCK, str(exc))]
            result['decision'] = {'outcome': 'reject', 'reason': str(exc), 'by': 'ingestion'}
            result['status'] = 'rejected'
            emit('orchestrator', 'stopped', str(exc), None)
        except ModelError as exc:
            # A model failure never falls back to approval; the run is recoverable.
            result['status'], result['error'] = 'error', str(exc)
            emit('orchestrator', 'error', f'Model call failed: {exc}. No payment was made; recover and resubmit.', None)
        except Exception as exc:  # keep a visible record instead of a stuck "processing" row
            result['status'] = 'error'
            result['error'] = f'Unexpected {type(exc).__name__}; no payment was made'
            emit('orchestrator', 'error', result['error'], None)
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        if self.model:
            result['usage'] = {**self.model.usage, 'api_calls': self.model.calls}
        self.store.checkpoint(result)
        return result

    def _settle(self, result, decision, advice, emit):
        invoice = result['invoice']
        with self.store.transaction() as db:
            if decision == 'approve':
                # Recheck stock, ledger and vendor at commit time: model calls may have taken seconds.
                fresh = tools.validate(db, invoice, None, result['id'])
                late = [f for f in fresh if f['severity'] in (BLOCK, HOLD) and f['code'] not in {x['code'] for x in result['findings']}]
                if late:
                    result['findings'] += late
                    decision = floor_decision(late)
                    emit('validation', 'commit_recheck', 'Changed while agents were reasoning: ' + summarize(late), late)
            findings = result['findings']
            blocks = [f for f in findings if f['severity'] == BLOCK]
            if decision == 'approve':
                reason = advice['reason']
            elif decision == 'reject':
                reason = summarize(findings, (BLOCK,))
            else:
                reason = summarize(findings, (HOLD,))
            result['decision'] = {'outcome': decision, 'reason': reason, 'by': advice['by'] if decision == 'approve' else 'policy gate'}
            result['status'] = STATUS[decision]
            if blocks and all(f['code'] == 'DUPLICATE_PAID' for f in blocks):
                result['status'] = 'duplicate'
            if decision == 'approve':
                self._pay(db, result, emit)
            elif decision == 'reject':
                emit('payment', 'rejection_logged', reason, None)
            else:
                emit('review', 'queued', 'Awaiting human review: ' + reason, None)
            Store.save(db, result)

    def _pay(self, db, result, emit):
        invoice = result['invoice']
        reference = 'MOCK-' + uuid.uuid4().hex[:16]
        db.execute('INSERT INTO payments(identity, run_id, vendor, amount_cents, currency, reference) VALUES (?,?,?,?,?,?)',
                   (result['identity'], result['id'], invoice['vendor'], invoice['total_cents'], invoice['currency'], reference))
        response = mock_payment(invoice['vendor'], f'{invoice["currency"]} {usd(invoice["total_cents"])}')
        result['status'] = 'paid'
        result['payment'] = {'reference': reference, 'amount': usd(invoice['total_cents']), 'currency': invoice['currency'],
                             'response': response, 'simulated': True}
        emit('payment', 'mock_payment', f'Paid {invoice["currency"]} {usd(invoice["total_cents"])} to {invoice["vendor"]} ({reference})',
             result['payment'])

    @staticmethod
    def _finding_summary(findings, floor):
        counts = {s: sum(f['severity'] == s for f in findings) for s in (BLOCK, HOLD)}
        if not counts[BLOCK] and not counts[HOLD]:
            return 'All checks passed'
        return f'{counts[BLOCK]} blocking, {counts[HOLD]} needing review -> minimum outcome {floor}'

    # ---- human in the loop ---------------------------------------------------------------
    def review(self, run_id, decision, reviewer, reason):
        if decision not in ('approve', 'reject') or not isinstance(reviewer, str) or not reviewer.strip() \
                or not isinstance(reason, str) or not reason.strip():
            raise InputError('Review requires approve/reject, a reviewer name and a reason')
        with self.store.transaction() as db:
            row = db.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = json.loads(row['result'])
            if result['status'] != 'pending_review':
                raise InputError('Only invoices awaiting review can be decided')
            entry = {'at': datetime.now(timezone.utc).isoformat(), 'agent': 'human', 'action': decision,
                     'summary': f'{reviewer.strip()}: {reason.strip()}', 'detail': {'reviewer': reviewer.strip()}}
            if decision == 'approve':
                fresh = [f for f in tools.validate(db, result['invoice'], None, result['id']) if f['severity'] == BLOCK]
                if fresh:
                    raise InputError('Cannot approve: ' + summarize(fresh, (BLOCK,)))
            result['events'].append(entry)
            result['review'] = {'decision': decision, 'reviewer': reviewer.strip(), 'reason': reason.strip(), 'at': entry['at']}
            result['decision'] = {'outcome': 'approve' if decision == 'approve' else 'reject', 'reason': reason.strip(),
                                  'by': f'human: {reviewer.strip()}'}
            result['status'] = 'rejected'
            if decision == 'approve':
                self._pay(db, result, lambda agent, action, summary, detail=None: result['events'].append(
                    {'at': datetime.now(timezone.utc).isoformat(), 'agent': agent, 'action': action, 'summary': summary, 'detail': detail}))
            Store.save(db, result)
            return result

    def recover(self, run_id):
        """Operator action for interrupted/error runs; releases the source claim so the file can be resubmitted."""
        with self.store.transaction() as db:
            row = db.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = json.loads(row['result'])
            if result['status'] not in ('processing', 'error'):
                raise InputError('Only interrupted or failed runs can be recovered')
            db.execute('UPDATE runs SET source_hash=? WHERE id=?', ('recovered:' + uuid.uuid4().hex, run_id))
            result['status'] = 'error'
            result['events'].append({'at': datetime.now(timezone.utc).isoformat(), 'agent': 'operator', 'action': 'recovered',
                                     'summary': 'Source claim released; resubmit the original file', 'detail': None})
            Store.save(db, result)
            return result
