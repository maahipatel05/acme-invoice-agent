#!/usr/bin/env python3
"""Acme invoice operations. All payments are simulated.

  python main.py --invoice_path=data/invoices/invoice_1001.txt     one invoice; logs on stderr, JSON result on stdout
  python main.py --batch data/invoices                             every invoice in a folder, with a summary table
  python main.py --serve                                           review console at http://127.0.0.1:8080
  python main.py --queue / --ledger                                invoices awaiting review / simulated payments
  add --mode gemini to use Gemini agents (needs GEMINI_API_KEY in .env)
"""
import argparse
import json
import re
import sys
from pathlib import Path
from acme.config import load_env
from acme.domain import InputError, usd
from acme.gemini import Gemini, ModelError
from acme.ingestion import EXTENSIONS, MAX_BYTES
from acme.store import Store
from acme.workflow import Workflow


def resolve(path):
    """The brief's example command uses data/invoices/invoice1.txt; the supplied files start at invoice_1001."""
    if not path.exists():
        match = re.fullmatch(r'invoice(\d{1,2})\.(\w+)', path.name)
        alias = path.with_name(f'invoice_{1000 + int(match[1])}.{match[2]}') if match else None
        if alias and alias.exists():
            print(f'note: {path} not found; using {alias} (the supplied invoices are numbered from 1001)', file=sys.stderr)
            return alias
    return path


def log_line(entry):
    print(json.dumps(entry), file=sys.stderr, flush=True)


def model_for(mode):
    return Gemini() if mode == 'gemini' else None


def batch(store, mode, folder, quiet):
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in EXTENSIONS)
    rows = []
    for path in files:
        result = Workflow(store, model_for(mode), None if quiet else log_line).process(path.name, path.read_bytes()[:MAX_BYTES + 1])
        invoice = result.get('invoice') or {}
        total = invoice.get('total_cents')
        reason = (result.get('decision') or {}).get('reason') or result.get('error') or ''
        rows.append((path.name, result['status'], f'{invoice.get("currency") or ""} {usd(total)}'.strip() if total is not None else '-',
                     ', '.join(sorted({f['code'] for f in result.get('findings', []) if f['severity'] != 'info'})) or '-', reason))
    width = max(len(r[0]) for r in rows) if rows else 10
    print(f'{"file":<{width}}  {"status":<15} {"amount":>14}  findings')
    for name, status, amount, codes, _ in rows:
        print(f'{name:<{width}}  {status:<15} {amount:>14}  {codes}')
    counts = {}
    for row in rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    print('\n' + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())) + f'  ({len(rows)} files, mode {mode})')
    return 0


def queue(store):
    pending = [r for r in store.all() if r['status'] == 'pending_review']
    if not pending:
        print('No invoices are awaiting review.')
    for r in pending:
        invoice = r.get('invoice') or {}
        print(f'{r["id"]}  {invoice.get("invoice_number")}  {invoice.get("vendor")}  '
              f'{invoice.get("currency")} {usd(invoice["total_cents"]) if invoice.get("total_cents") is not None else "-"}')
        print(f'    {r["decision"]["reason"]}')
    return 0


def ledger(store):
    with store.connect() as db:
        rows = db.execute('SELECT * FROM payments ORDER BY created').fetchall()
    if not rows:
        print('No simulated payments yet.')
    for row in rows:
        print(f'{row["created"]}  {row["reference"]}  {row["currency"]} {usd(row["amount_cents"]):>12}  {row["vendor"]}')
    return 0


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Acme invoice operations (simulated payments only)')
    parser.add_argument('--invoice_path', type=Path, help='process one invoice file')
    parser.add_argument('--batch', type=Path, metavar='DIR', help='process every invoice file in a folder')
    parser.add_argument('--serve', action='store_true', help='start the local review console')
    parser.add_argument('--mode', choices=['offline', 'gemini'], default='offline')
    parser.add_argument('--db', default='.runtime/acme.db', help='SQLite workspace (created and seeded automatically)')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bind', default='127.0.0.1', help='console bind address (Docker uses 0.0.0.0)')
    parser.add_argument('--review', metavar='RUN_ID', help='decide an invoice awaiting review')
    parser.add_argument('--decision', choices=['approve', 'reject'])
    parser.add_argument('--reviewer')
    parser.add_argument('--reason')
    parser.add_argument('--recover', metavar='RUN_ID', help='release an interrupted/failed run so it can be resubmitted')
    parser.add_argument('--queue', action='store_true', help='list invoices awaiting review, with run IDs')
    parser.add_argument('--ledger', action='store_true', help='list simulated payments')
    parser.add_argument('--list-models', action='store_true', help='list Gemini models available to your key')
    parser.add_argument('--quiet', action='store_true', help='no per-step logs on stderr')
    args = parser.parse_args()
    try:
        if args.list_models:
            print(json.dumps(Gemini().list_models(), indent=2))
            return 0
        store = Store(args.db)
        if args.serve:
            from acme.server import serve
            serve(store, args.mode, args.port, args.bind)
            return 0
        if args.batch:
            return batch(store, args.mode, args.batch, args.quiet)
        if args.queue:
            return queue(store)
        if args.ledger:
            return ledger(store)
        workflow = Workflow(store, model_for(args.mode), None if args.quiet else log_line)
        if args.review:
            result = workflow.review(args.review, args.decision, args.reviewer, args.reason)
        elif args.recover:
            result = workflow.recover(args.recover)
        elif args.invoice_path:
            path = resolve(args.invoice_path)
            with path.open('rb') as source:
                result = workflow.process(path.name, source.read(MAX_BYTES + 1))
        else:
            parser.error('Supply --invoice_path, --batch, --serve, --queue, --ledger, --review or --recover')
        print(json.dumps(result, indent=2))
        return 2 if result['status'] == 'error' else 0
    except KeyError as exc:
        print(json.dumps({'status': 'error', 'error': f'No run with ID {exc.args[0]}; see --queue for IDs'}), file=sys.stderr)
        return 2
    except (InputError, ModelError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
