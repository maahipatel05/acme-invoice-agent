#!/usr/bin/env python3
"""Evaluate the system against golden expectations and write a report.

  python3 evals/run.py                    offline, no key needed (about a second)
  python3 evals/run.py --mode gemini      live agents; writes reports/eval-gemini.md

Scores: outcome accuracy, finding-code recall (did we flag what the brief expects),
extraction field accuracy, and cost/latency per invoice in Gemini mode.
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from acme.config import load_env
from acme.domain import key, money
from acme.gemini import Gemini
from acme.store import Store
from acme.workflow import Workflow

GOLDEN = json.loads((ROOT / 'evals/golden.json').read_text())


def field_checks(result, fields):
    """Compare the normalized invoice with values transcribed from the source by hand."""
    invoice = result.get('invoice')
    if not invoice:
        return []
    checks = [('invoice_number', key(invoice['invoice_number'] or '') == key(fields['invoice_number'] or '')),
              ('vendor', (invoice['vendor'] or None) == fields['vendor']),
              ('due_date', invoice['due_date'] == fields['due_date']),
              ('total', invoice['total_cents'] == money(fields['total']))]
    actual = sorted((key(l['item'] or ''), l['quantity'], l['unit_price_cents']) for l in invoice['items'])
    wanted = sorted((key(n), q, money(p)) for n, q, p in fields['lines'])
    checks += [(f'line {i + 1}', i < len(actual) and actual[i] == w) for i, w in enumerate(wanted)]
    return checks


def evaluate(path, expected, store, mode):
    model = Gemini() if mode == 'gemini' else None
    data = path.read_bytes()
    started = time.monotonic()
    result = Workflow(store, model).process(path.name, data)
    if mode == 'gemini' and result['status'] == 'error' and 'HTTP 429' in (result.get('error') or ''):
        # Free-tier per-minute quota: wait for the window, release the run, retry once (what an operator would do).
        print(f'{path.name}: rate limited; waiting 65s and retrying once', file=sys.stderr, flush=True)
        time.sleep(65)
        Workflow(store).recover(result['id'])
        model = Gemini()
        result = Workflow(store, model).process(path.name, data)
    status = expected['status'][mode] if isinstance(expected['status'], dict) else expected['status']
    codes = {f['code'] for f in result.get('findings', [])}
    missing = [c for c in expected['codes'] if c not in codes]
    fields = field_checks(result, expected['fields']) if 'fields' in expected else []
    row = {'file': path.name, 'expected': status, 'actual': result['status'], 'status_ok': result['status'] == status,
           'codes_expected': expected['codes'], 'codes_missing': missing,
           'codes': sorted(c for c in codes if c != 'CURRENCY_ASSUMED_USD'),
           'fields_ok': sum(ok for _, ok in fields), 'fields_total': len(fields),
           'fields_wrong': [name for name, ok in fields if not ok],
           'seconds': round(time.monotonic() - started, 1), 'error': result.get('error'),
           'decision': (result.get('decision') or {}).get('reason'),
           'model_calls': model.calls if model else 0, 'tokens': sum(model.usage.values()) if model else 0}
    if 'sha256' in expected:
        row['source_unchanged'] = hashlib.sha256(data).hexdigest() == expected['sha256']
    return row


def run(mode, only=None, progress=True):
    rows = {'official': [], 'scenarios': []}
    with tempfile.TemporaryDirectory(prefix='acme-eval-') as folder:
        shared = Store(Path(folder) / 'official.db')        # one ledger: revisions and duplicates must be caught
        for name, expected in sorted(GOLDEN['official'].items()):
            if only and name not in only:
                continue
            rows['official'].append(evaluate(ROOT / 'data/invoices' / name, expected, shared, mode))
            if progress:
                print(f'{name:28} {rows["official"][-1]["actual"]}', file=sys.stderr, flush=True)
        for name, expected in sorted(GOLDEN['scenarios'].items()):
            if only and name not in only:
                continue
            rows['scenarios'].append(evaluate(ROOT / 'data/scenarios' / name, expected,
                                              Store(Path(folder) / f'{name}.db'), mode))
            if progress:
                print(f'{name:28} {rows["scenarios"][-1]["actual"]}', file=sys.stderr, flush=True)
    return rows


def summary(rows):
    every = rows['official'] + rows['scenarios']
    expected_codes = sum(len(r['codes_expected']) for r in every)
    return {'invoices': len(every),
            'outcomes_correct': sum(r['status_ok'] for r in every),
            'finding_recall': f'{expected_codes - sum(len(r["codes_missing"]) for r in every)}/{expected_codes}',
            'extraction_fields': f'{sum(r["fields_ok"] for r in every)}/{sum(r["fields_total"] for r in every)}',
            'model_calls': sum(r['model_calls'] for r in every), 'tokens': sum(r['tokens'] for r in every),
            'seconds_per_invoice': round(sum(r['seconds'] for r in every) / max(len(every), 1), 1),
            'errors': sum(1 for r in every if r['error'])}


def markdown(mode, model, rows, totals):
    lines = [f'# Evaluation: {mode} mode', '',
             f'Run {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}' + (f' with `{model}`' if model else '') + '.', '',
             f'- Outcomes correct: **{totals["outcomes_correct"]}/{totals["invoices"]}**',
             f'- Expected findings raised: **{totals["finding_recall"]}**',
             f'- Extraction fields correct (official invoices): **{totals["extraction_fields"]}**',
             f'- Average time per invoice: {totals["seconds_per_invoice"]}s'
             + (f'; {totals["model_calls"]} model calls, {totals["tokens"]:,} tokens in total' if mode == 'gemini' else ''), '']
    for group, title in (('official', 'Supplied invoices (one shared ledger, filename order)'), ('scenarios', 'Added scenarios')):
        if not rows[group]:
            continue
        lines += [f'## {title}', '', '| File | Expected | Actual | Findings | Fields | Time |', '|---|---|---|---|---|---|']
        for r in rows[group]:
            mark = '' if r['status_ok'] and not r['codes_missing'] else ' **MISMATCH**'
            fields = f'{r["fields_ok"]}/{r["fields_total"]}' if r['fields_total'] else '-'
            lines.append(f'| {r["file"]} | {r["expected"]} | {r["actual"]}{mark} | {", ".join(r["codes"]) or "-"} | {fields} | {r["seconds"]}s |')
        lines.append('')
    misses = [r for g in rows.values() for r in g if not r['status_ok'] or r['codes_missing'] or r['fields_wrong']]
    if misses:
        lines += ['## Differences', '']
        lines += [f'- {r["file"]}: expected {r["expected"]}, got {r["actual"]}'
                  + (f'; missing {", ".join(r["codes_missing"])}' if r['codes_missing'] else '')
                  + (f'; wrong fields {", ".join(r["fields_wrong"])}' if r['fields_wrong'] else '')
                  + (f'; error: {r["error"]}' if r['error'] else '') for r in misses]
    return '\n'.join(lines) + '\n'


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mode', choices=['offline', 'gemini'], default='offline')
    parser.add_argument('--only', nargs='+', help='evaluate selected files only (report is not written)')
    args = parser.parse_args()
    rows = run(args.mode, args.only)
    totals = summary(rows)
    model = Gemini().model if args.mode == 'gemini' else None
    report = markdown(args.mode, model, rows, totals)
    print(report)
    if not args.only:
        out = ROOT / 'reports'
        out.mkdir(exist_ok=True)
        (out / f'eval-{args.mode}.md').write_text(report)
        (out / f'eval-{args.mode}.json').write_text(json.dumps({'mode': args.mode, 'model': model, 'summary': totals,
                                                                 **rows}, indent=1) + '\n')
    every = rows['official'] + rows['scenarios']
    return 0 if all(r['status_ok'] and not r['codes_missing'] for r in every) else 1


if __name__ == '__main__':
    sys.exit(main())
