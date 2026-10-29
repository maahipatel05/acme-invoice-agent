"""Read-only business tools over SQLite.

The deterministic validator and the LLM agents call these same functions, so a
model's tool call and the code's check always see identical data.
"""
import difflib
import json
import re
from .domain import BLOCK, HOLD, finding, key, usd

APPROVAL_LIMIT_CENTS = 1_000_000          # invoices strictly over $10,000 need VP sign-off
PRICE_TOLERANCE = 0.10                    # billed unit price may exceed catalog by at most 10%
SPLIT_WINDOW_DAYS = 30                    # invoices from one vendor in this window are summed for split billing
PRESSURE = [r'\burgent\b', r'pay immediately', r'wire transfer', r'avoid penalt\w*', r'change (?:of|in|to) (?:our )?bank',
            r'new (?:bank|account) (?:details|number)', r'gift card']
INJECTION = [r'ignore (?:all |any )?(?:prior|previous|above) (?:instructions|rules)', r'\bsystem\s*:',
             r'approve (?:this |the )?(?:payment|invoice)', r'disregard [^.\n]{0,40}(?:rules|instructions|policy)',
             r'you are (?:now )?(?:an?|the) (?:assistant|model|ai|agent)']


def identity(vendor, number):
    return f'{key(vendor)}:{key(number)}' if vendor and number else None


def approval_policy():
    return {'approval_limit_usd': '10,000.00', 'rule': 'Invoices strictly over the limit need VP sign-off',
            'price_tolerance_percent': int(PRICE_TOLERANCE * 100), 'settlement_currency': 'USD',
            'hard_rules': 'Unknown items, zero stock, quantity over stock, bad arithmetic, invalid data and '
                          'already-paid invoices are rejected and cannot be overridden by any agent or reviewer'}


def lookup_item(db, name):
    rows = [dict(r) for r in db.execute('SELECT item, stock, unit_price_cents FROM inventory')]
    for row in rows:
        if key(row['item']) == key(name):
            return {'found': True, 'item': row['item'], 'stock': row['stock'],
                    'catalog_unit_price': usd(row['unit_price_cents']) if row['unit_price_cents'] else None}
    close = difflib.get_close_matches(key(name), [key(r['item']) for r in rows], n=3, cutoff=0.75)
    similar = [r['item'] for c in close for r in rows if key(r['item']) == c]
    return {'found': False, 'requested': name, 'similar_items': similar,
            'note': 'Similar names are shown for context only; they are never substituted'}


def check_vendor(db, name):
    vendors = [r['name'] for r in db.execute('SELECT name FROM vendors')]
    for vendor in vendors:
        if key(vendor) == key(name):
            return {'status': 'approved', 'vendor': vendor}
    scored = sorted(((difflib.SequenceMatcher(None, key(name), key(v)).ratio(), v) for v in vendors), reverse=True)
    if scored and scored[0][0] >= 0.8:
        return {'status': 'lookalike', 'requested': name, 'similar_to': scored[0][1], 'similarity': round(scored[0][0], 2)}
    return {'status': 'unknown', 'requested': name}


def payment_history(db, vendor, invoice_number, exclude_run=None):
    ident = identity(vendor, invoice_number)
    if ident is None:
        return {'paid': None, 'open_runs': []}
    paid = db.execute('SELECT run_id, amount_cents, currency, reference, created FROM payments WHERE identity=?',
                      (ident,)).fetchone()
    open_runs = [r['id'] for r in db.execute(
        "SELECT id FROM runs WHERE identity=? AND status='pending_review' AND id IS NOT ?", (ident, exclude_run))]
    return {'paid': {'run_id': paid['run_id'], 'amount': usd(paid['amount_cents']), 'currency': paid['currency'],
                     'reference': paid['reference'], 'paid_at': paid['created']} if paid else None,
            'open_runs': open_runs}


def recent_vendor_spend(db, vendor, invoice_number, exclude_run=None):
    """Other paid or pending invoices from this vendor in the split-billing window (same invoice excluded)."""
    ident = identity(vendor, invoice_number)
    if ident is None:
        return {'invoices': [], 'total_cents': 0}
    prefix = key(vendor) + ':'
    rows = db.execute(
        "SELECT identity, amount_cents FROM payments WHERE substr(identity, 1, ?)=? AND identity<>? "
        f"AND created >= datetime('now', '-{SPLIT_WINDOW_DAYS} days')", (len(prefix), prefix, ident)).fetchall()
    seen = {r['identity']: r['amount_cents'] for r in rows}
    for row in db.execute("SELECT identity, result FROM runs WHERE status='pending_review' AND substr(identity, 1, ?)=? "
                          f"AND identity<>? AND id IS NOT ? AND created >= datetime('now', '-{SPLIT_WINDOW_DAYS} days')",
                          (len(prefix), prefix, ident, exclude_run)):
        amount = (json.loads(row['result']).get('invoice') or {}).get('total_cents') or 0
        seen.setdefault(row['identity'], amount)
    small = {k: v for k, v in seen.items() if 0 < v <= APPROVAL_LIMIT_CENTS}
    return {'invoices': sorted(k.split(':', 1)[1] for k in small), 'total_cents': sum(small.values())}


def validate(db, invoice, source_text=None, run_id=None):
    """Deterministic validation agent: returns findings for inventory, vendor, policy, duplicates and fraud signals."""
    findings = []
    requested, lines_seen = {}, {}
    for line in invoice['items']:
        if line['item'] and line['quantity'] is not None and line['quantity'] > 0:
            requested[key(line['item'])] = requested.get(key(line['item']), 0) + line['quantity']
            lines_seen.setdefault(key(line['item']), []).append(line)
    for item_key, quantity in requested.items():
        name = lines_seen[item_key][0]['item']
        info = lookup_item(db, name)
        if not info['found']:
            hint = f' (similar: {", ".join(info["similar_items"])}; not substituted)' if info['similar_items'] else ''
            findings.append(finding('UNKNOWN_ITEM', BLOCK, f'{name} is not in inventory{hint}', item=name))
            continue
        across = f' across {len(lines_seen[item_key])} lines' if len(lines_seen[item_key]) > 1 else ''
        if info['stock'] == 0:
            findings.append(finding('ZERO_STOCK_ITEM', BLOCK,
                f'{info["item"]} has zero stock (requested {quantity}); placeholder or fraudulent item', item=info['item']))
        elif quantity > info['stock']:
            findings.append(finding('STOCK_EXCEEDED', BLOCK,
                f'{info["item"]}: {quantity} requested{across}, only {info["stock"]} in stock', item=info['item']))
        row = db.execute('SELECT unit_price_cents FROM inventory WHERE item=?', (info['item'],)).fetchone()
        catalog = row['unit_price_cents'] if row else None
        for line in lines_seen[item_key]:
            price = line['unit_price_cents']
            if catalog and price and price > catalog * (1 + PRICE_TOLERANCE):
                label = info['item'] + (f' ({line["description"]})' if line.get('description') else '')
                findings.append(finding('PRICE_VARIANCE', HOLD,
                    f'{label} billed at {usd(price)} vs catalog {usd(catalog)} (+{round((price / catalog - 1) * 100)}%)',
                    item=info['item']))

    if invoice['vendor']:
        vendor = check_vendor(db, invoice['vendor'])
        renamed = re.search(r'formerly\s+([^)\n]+)', source_text or '', re.I)
        former = check_vendor(db, renamed.group(1).strip().rstrip('.')) if renamed else None
        if vendor['status'] == 'approved':
            pass
        elif former and former['status'] == 'approved':
            findings.append(finding('VENDOR_RENAMED', HOLD,
                f'{invoice["vendor"]} is not in the vendor master; the invoice says it was formerly {former["vendor"].rstrip(".")}; '
                'verify the rename and bank details before paying'))
        elif vendor['status'] == 'lookalike':
            findings.append(finding('VENDOR_LOOKALIKE', HOLD,
                f'{invoice["vendor"]} closely resembles approved vendor {vendor["similar_to"]} but does not match'))
        else:
            findings.append(finding('UNKNOWN_VENDOR', HOLD, f'{invoice["vendor"]} is not in the approved vendor master'))

    total = invoice['total_cents']
    if total is not None and total > APPROVAL_LIMIT_CENTS:
        findings.append(finding('OVER_APPROVAL_LIMIT', HOLD,
                                f'Total {invoice["currency"] or ""} {usd(total)} exceeds the $10,000.00 approval limit; VP sign-off required'))
    if invoice['currency'] and invoice['currency'] != 'USD':
        findings.append(finding('NON_USD_CURRENCY', HOLD,
                                f'Invoice is in {invoice["currency"]}; confirm the settlement amount before paying'))

    split = recent_vendor_spend(db, invoice['vendor'], invoice['invoice_number'], run_id)
    if total is not None and 0 < total <= APPROVAL_LIMIT_CENTS and split['invoices'] and \
            split['total_cents'] + total > APPROVAL_LIMIT_CENTS:
        findings.append(finding('SPLIT_INVOICE', HOLD,
            f'With {len(split["invoices"])} other invoice(s) from {invoice["vendor"]} in the last {SPLIT_WINDOW_DAYS} days '
            f'({usd(split["total_cents"])}), the total reaches {usd(split["total_cents"] + total)}, above the $10,000.00 '
            'approval limit; check for split billing', other_invoices=split['invoices']))

    history = payment_history(db, invoice['vendor'], invoice['invoice_number'], run_id)
    if history['paid']:
        paid = history['paid']
        findings.append(finding('DUPLICATE_PAID', BLOCK,
            f'{invoice["invoice_number"]} from {invoice["vendor"]} was already paid ({paid["currency"]} {paid["amount"]}, '
            f'{paid["reference"]}); a resubmission or revision cannot be paid again', original_run_id=paid['run_id']))
    elif history['open_runs']:
        findings.append(finding('DUPLICATE_PENDING', HOLD,
            f'{invoice["invoice_number"]} from {invoice["vendor"]} is already awaiting review in another run',
            other_runs=history['open_runs']))

    text = (source_text or '').casefold()
    pressure = sorted({m.group(0) for p in PRESSURE for m in re.finditer(p, text)})
    if pressure:
        findings.append(finding('PAYMENT_PRESSURE', HOLD, 'Payment-pressure language: ' + ', '.join(f'"{p}"' for p in pressure)))
    if any(re.search(p, text) for p in INJECTION):
        findings.append(finding('PROMPT_INJECTION', HOLD, 'Document contains instructions aimed at the approval system; treated as data'))
    return findings
