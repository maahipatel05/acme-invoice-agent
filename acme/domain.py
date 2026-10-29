"""Untrusted invoice data becomes a canonical invoice plus coded findings.

Money never uses floating point. Normalization does not stop at the first
problem: every defect it can see is reported, so an operator gets the full
picture in one pass (INV-1009 is missing a vendor AND has a negative quantity).
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

BLOCK, HOLD, INFO = 'block', 'hold', 'info'
SEVERITY_RANK = {INFO: 0, HOLD: 1, BLOCK: 2}
RELATIVE_DATES = {'yesterday', 'today', 'tomorrow', 'asap', 'immediately', 'now', 'upon receipt', 'on receipt'}


class InputError(ValueError):
    """The document cannot be read as one invoice at all (not a business finding)."""


def finding(code, severity, message, **detail):
    return {'code': code, 'severity': severity, 'message': message, **detail}


def money(value):
    if isinstance(value, bool) or value is None:
        raise InputError('Missing or invalid monetary value')
    literal = str(value).strip()
    if not re.fullmatch(r'-?\$?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?', literal):
        raise InputError(f'Invalid money format: {literal[:40]!r}')
    try:
        result = Decimal(literal.replace('$', '').replace(',', ''))
    except InvalidOperation as exc:
        raise InputError('Invalid monetary value') from exc
    if abs(result) > Decimal('1000000000'):
        raise InputError('Money must be within supported limits')
    return int(result * 100)


def usd(cents):
    """Display helper: 1234567 -> '12,345.67'."""
    sign = '-' if cents < 0 else ''
    return f'{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}'


def key(value):
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def _text(value, limit):
    if isinstance(value, str) and key(value) and len(value) <= limit:
        return value.strip()
    return None


def _money_field(value, label, findings, allow_absent=False):
    if allow_absent and (value is None or value == ''):
        return 0
    if value is None or value == '':
        findings.append(finding('MISSING_AMOUNT', BLOCK, f'{label} is missing'))
        return None
    try:
        return money(value)
    except InputError:
        findings.append(finding('INVALID_AMOUNT', BLOCK, f'{label} {str(value)[:40]!r} is not a valid amount'))
        return None


def normalize(raw):
    """Return (invoice, findings). Raises InputError only for unreadable structure."""
    if not isinstance(raw, dict):
        raise InputError('Expected one invoice object')
    for names in [('items', 'line_items'), ('total', 'amount')]:
        if all(name in raw for name in names):
            raise InputError('Conflicting invoice field aliases; provide only one representation')
    findings = []

    vendor = raw.get('vendor')
    if isinstance(vendor, dict):
        vendor = vendor.get('name')
    vendor = _text(vendor, 200)
    if vendor is None:
        findings.append(finding('MISSING_VENDOR', BLOCK, 'Vendor name is missing'))
    number = raw.get('invoice_number')
    number = _text(str(number) if isinstance(number, int) and not isinstance(number, bool) else number, 100)
    if number is None:
        findings.append(finding('MISSING_INVOICE_NUMBER', BLOCK, 'Invoice number is missing'))

    due_raw = raw.get('due_date')
    due = None
    if due_raw is None or (isinstance(due_raw, str) and not due_raw.strip()):
        findings.append(finding('MISSING_DUE_DATE', BLOCK, 'Due date is missing'))
    elif isinstance(due_raw, str) and due_raw.strip().casefold() in RELATIVE_DATES:
        findings.append(finding('RELATIVE_DUE_DATE', BLOCK,
                                f'Due date "{due_raw.strip()}" is relative, not a calendar date; a common pressure tactic'))
    else:
        try:
            if not isinstance(due_raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', due_raw.strip()):
                raise ValueError
            due = date.fromisoformat(due_raw.strip()).isoformat()
        except ValueError:
            findings.append(finding('INVALID_DUE_DATE', BLOCK, f'Due date {str(due_raw)[:40]!r} is not a valid date'))

    rows = raw.get('line_items', raw.get('items'))
    lines = []
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        findings.append(finding('NO_LINE_ITEMS', BLOCK, 'Invoice must contain 1-200 line items'))
        rows = []
    lines_complete = True
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise InputError('Invalid line item')
        if 'item' in row and 'name' in row:
            raise InputError('Conflicting line item aliases')
        name = _text(row.get('item', row.get('name')), 150)
        label = f'Line {index} ({name or "unnamed"})'
        line = {'item': name, 'quantity': None, 'unit_price_cents': None}
        if name is None:
            findings.append(finding('INVALID_LINE', BLOCK, f'Line {index} has no item name'))
        quantity = row.get('quantity')
        if isinstance(quantity, float) and quantity.is_integer():
            quantity = int(quantity)
        literal = str(quantity).strip() if quantity is not None and not isinstance(quantity, bool) else ''
        if re.fullmatch(r'-?[0-9]{1,7}', literal):
            line['quantity'] = int(literal)
            if line['quantity'] <= 0:
                findings.append(finding('INVALID_QUANTITY', BLOCK,
                                        f'{label}: quantity {line["quantity"]} must be a positive whole number'))
        else:
            findings.append(finding('INVALID_QUANTITY', BLOCK, f'{label}: quantity {literal[:20]!r} is not a whole number'))
        price = _money_field(row.get('unit_price'), f'{label} unit price', findings)
        if price is not None and price <= 0:
            findings.append(finding('INVALID_AMOUNT', BLOCK, f'{label}: unit price {usd(price)} must be positive'))
        line['unit_price_cents'] = price
        if isinstance(row.get('description'), str) and row['description'].strip():
            line['description'] = row['description'].strip()[:1000]
        if row.get('amount') not in (None, ''):
            stated = _money_field(row['amount'], f'{label} amount', findings)
            if stated is not None:
                line['amount_cents'] = stated
                if None not in (price, line['quantity']) and stated != price * line['quantity']:
                    findings.append(finding('LINE_AMOUNT_MISMATCH', BLOCK,
                        f'{label}: stated amount {usd(stated)} differs from {line["quantity"]} x {usd(price)}'))
        if line['quantity'] is None or price is None:
            lines_complete = False
        lines.append(line)

    total = _money_field(raw.get('total', raw.get('amount')), 'Invoice total', findings)
    if total is not None and total <= 0:
        findings.append(finding('INVALID_AMOUNT', BLOCK, f'Invoice total {usd(total)} must be positive'))
    tax = _money_field(raw.get('tax_amount'), 'Tax', findings, allow_absent=True)
    shipping = _money_field(raw.get('shipping_amount'), 'Shipping', findings, allow_absent=True)
    for label, value in (('Tax', tax), ('Shipping', shipping)):
        if value is not None and value < 0:
            findings.append(finding('INVALID_AMOUNT', BLOCK, f'{label} {usd(value)} cannot be negative'))

    subtotal = sum(x['quantity'] * x['unit_price_cents'] for x in lines) if lines and lines_complete else None
    if subtotal is not None and raw.get('subtotal') not in (None, ''):
        stated = _money_field(raw['subtotal'], 'Subtotal', findings)
        if stated is not None and stated != subtotal:
            findings.append(finding('SUBTOTAL_MISMATCH', BLOCK,
                                    f'Stated subtotal {usd(stated)} differs from line items {usd(subtotal)}'))
    if None not in (subtotal, total, tax, shipping):
        expected = subtotal + tax + shipping
        if total != expected:
            parts = f'lines {usd(subtotal)}' + (f' + tax {usd(tax)}' if tax else '') + (f' + shipping {usd(shipping)}' if shipping else '')
            findings.append(finding('TOTAL_MISMATCH', BLOCK,
                f'Stated total {usd(total)} does not equal {parts} = {usd(expected)} (off by {usd(abs(total - expected))})'))

    currency = raw.get('currency')
    currency_assumed = False
    if currency in (None, ''):
        # Acme is a US company that settles in USD; record the assumption rather than hide it.
        currency, currency_assumed = 'USD', True
        findings.append(finding('CURRENCY_ASSUMED_USD', INFO, 'No currency code stated; treated as USD, Acme\'s settlement currency'))
    elif isinstance(currency, str) and re.fullmatch(r'[A-Za-z]{3}', currency.strip()):
        currency = currency.strip().upper()
    else:
        findings.append(finding('INVALID_CURRENCY', HOLD, f'Currency {str(currency)[:20]!r} is not an ISO code; confirm settlement currency'))
        currency = None

    for note in raw.get('uncertainties') or []:
        findings.append(finding('EXTRACTION_UNCERTAINTY', HOLD, f'Extraction uncertainty: {str(note)[:300]}'))

    invoice = {'invoice_number': number, 'vendor': vendor, 'due_date': due, 'currency': currency,
               'currency_assumed': currency_assumed, 'total_cents': total, 'subtotal_cents': subtotal,
               'tax_cents': tax, 'shipping_cents': shipping, 'items': lines}
    return invoice, findings


def floor_decision(findings):
    """Deterministic minimum: any block rejects, any hold needs a human, else approve."""
    severities = {f['severity'] for f in findings}
    return 'reject' if BLOCK in severities else 'review' if HOLD in severities else 'approve'


def summarize(findings, severities=(BLOCK, HOLD)):
    return '; '.join(f['message'] for f in findings if f['severity'] in severities)
