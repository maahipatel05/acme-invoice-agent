"""Deterministic adapters for labelled text and common legacy CSV exports."""
import csv
import io
import re
from datetime import datetime
from .domain import InputError, key


def date_value(value):
    if not isinstance(value, str):
        return value
    numeric=re.fullmatch(r'(\d{1,2})/(\d{1,2})/\d{4}',value.strip())
    if numeric and int(numeric[1])<=12 and int(numeric[2])<=12 and numeric[1]!=numeric[2]:
        raise InputError('Ambiguous numeric due date; use ISO YYYY-MM-DD')
    for pattern in ('%Y-%m-%d', '%d-%b-%Y', '%B %d, %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    # Never resolve relative dates against the machine's current date.
    return value


def labelled_text(text):
    # PDF rows can hold two header cells ("Vendor: Atlas  Due: 2026-03-24"). Split
    # them onto separate lines; money rows are left alone.
    text = '\n'.join(line if '$' in line else re.sub(
        r'(?<!\bDue)(?<!\bInvoice)[ \t]+(?=(?:Due Date|Due|Date|Terms|Vendor|Invoice Number|Invoice)[ \t]*:)', '\n', line)
        for line in text.splitlines())

    def field(pattern, source=None):
        matches = list(re.finditer(pattern, text if source is None else source, re.I | re.M))
        if len(matches) > 1:
            raise InputError('Repeated invoice header field is ambiguous; correct the source')
        return matches[0].group(1).strip() if matches else None
    vendor_source = re.sub(r'(?im)^[ \t]*from[ \t]*:[^\n]*@[^\n]*(?:\n|$)', '', text)
    vendor = field(r'^[ \t]*(?:Vendor|Vndr|FROM)[ \t]*:[ \t]*([^\n]*)$', vendor_source)
    number = field(r'^[ \t]*(?:Invoice Number|Invoice|Inv #|INV NO)[ \t]*(?::|#)[ \t]*([^\n]*)$')
    # Embedded PDF text can put adjacent header cells on one line.
    if vendor:
        vendor = re.split(r'[ \t]+(?:Terms|Due(?: Date)?|Date)[ \t]*:', vendor, maxsplit=1, flags=re.I)[0].strip()
    if number:
        number = re.split(r'[ \t]+(?:Date|Due(?: Date)?)[ \t]*:', number, maxsplit=1, flags=re.I)[0].strip()
    due = field(r'^[ \t]*(?:Due Date|Due Dt|Due)[ \t]*:[ \t]*([^\n]*)$')
    result = {'vendor':vendor,'invoice_number':number,'due_date':date_value(due),
        'currency':field(r'^[ \t]*Currency[ \t]*:[ \t]*([^\n]*)$'),
        'total':field(r'^[ \t]*(?:Total Amount|Grand Total|Total|Amt)[ \t]*:[ \t]*\$?([^\n]+)$'),
        'tax_amount':field(r'^[ \t]*(?:Sales[ \t]+)?Tax(?:[ \t]*\([^)]*\))?[ \t]*:[ \t]*\$?([^\n]+)$'),
        'shipping_amount':field(r'^[ \t]*(?:Shipping|Freight)[ \t]*:[ \t]*\$?([^\n]+)$'),
        'items':[], 'uncertainties':[], 'normalizations':[]}
    # Subtotal may follow a table row on the same PDF-extracted line.
    result['subtotal'] = field(r'\bSubtotal[ \t]*:[ \t]*\$?([^\n]*)$')
    if due != result['due_date']:
        result['normalizations'].append({'field':'due_date','source':due,'value':result['due_date']})
    item_pattern = re.compile(
        r'(?<![A-Za-z0-9_])(?P<item>[A-Za-z][A-Za-z0-9]*(?:[ \t]+[A-Z](?![A-Za-z]))?)'
        r'(?:[ \t]+\((?P<description>[^()\n]+)\))?[ \t]+'
        r'(?:qty[ \t]*:?[ \t]*|[xX])?(?P<quantity>-?\d+)[ \t]+'
        r'(?:unit[ \t]+price[ \t]*:[ \t]*|@[ \t]*)?\$(?P<price>[\d,.]+)(?![\w.,])'
        r'(?:[ \t]+(?P<amount_sign>-?)\$(?P<amount>[\d,.Oo]+)(?![\w.,])|[ \t]+(?:ea|each))?', re.I)
    for line in text.splitlines():
        # PDF text extraction can merge the final item and totals cells. Those
        # explicitly labelled totals are parsed above, not ignored item charges.
        row_text=re.split(r'\bSubtotal\s*:',
                          line,maxsplit=1,flags=re.I)[0]
        previous_end=0
        matches=list(item_pattern.finditer(row_text))
        if matches and any(not any(m.start()<=position<m.end() for m in matches) for position,c in enumerate(row_text) if c=='$'):
            raise InputError('Unparsed monetary value on invoice item row')
        if not matches and re.search(r'-?\d+[ \t]+(?:unit[ \t]+price[ \t]*:[ \t]*|@[ \t]*)?-?\$',row_text,re.I):
            raise InputError('Malformed invoice item monetary token')
        for match in matches:
            name=match['item'].strip()
            prefix=line[previous_end:match.start()].strip(' \t-•')
            previous_end=match.end()
            # Preserve unknown multiword names instead of silently matching
            # their final known-SKU token. Only recognizable table labels vanish.
            header=bool(re.fullmatch(r'item\s+qty\s+(?:unit\s+)?price\s+(?:amount|total)',prefix,re.I))
            if prefix and not header:
                name=prefix+' '+name
            item={'item':name,'quantity':match['quantity'],'unit_price':match['price']}
            if match['description']:
                item['description']=match['description']
            if match['amount']:
                literal=(match['amount_sign'] or '')+match['amount']
                if 'o' in literal.lower():
                    proposed=literal.replace('O','0').replace('o','0')
                    item['source_amount']=literal
                    result['normalizations'].append({'field':'line_amount','source':literal,'value':proposed})
                    result['uncertainties'].append(f'OCR ambiguity in stated line amount {literal}; verify proposed {proposed}')
                    literal=proposed
                item['amount']=literal
            result['items'].append(item)
    return result


ALIASES = {'invoicenumber':'invoice_number','vendor':'vendor','date':'date','duedate':'due_date',
    'currency':'currency','item':'item','name':'item','qty':'quantity','quantity':'quantity',
    'unitprice':'unit_price','linetotal':'amount','amount':'amount','total':'total','totalamount':'total',
    'subtotal':'subtotal','tax':'tax_amount','taxamount':'tax_amount','shipping':'shipping_amount',
    'shippingamount':'shipping_amount','paymentterms':'payment_terms'}
METADATA = {'invoice_number','vendor','date','due_date','currency','total','subtotal','tax_amount','shipping_amount','payment_terms'}


def legacy_csv(text):
    rows=list(csv.reader(io.StringIO(text)))
    rows=[r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise InputError('Empty CSV')
    columns=[key(c) for c in rows[0]]
    if any(not c for c in columns) or len(set(columns))!=len(columns):
        raise InputError('CSV column names must be nonempty and unique')
    if any(len(r)!=len(columns) for r in rows[1:]):
        raise InputError('CSV row width differs from header')
    raw={'items':[]}
    def singleton(name,value):
        if name in raw:
            raise InputError('Repeated CSV invoice field is ambiguous')
        raw[name]=value
    if columns==['field','value']:
        current=None
        for label,value in rows[1:]:
            name=ALIASES.get(key(label))
            if name=='item':
                if current is not None:
                    raw['items'].append(current)
                current={'item':value}
            elif name in {'quantity','unit_price','amount'}:
                if current is None or name in current:
                    raise InputError('CSV item fields must follow an item exactly once')
                current[name]=value
            elif name in METADATA:
                singleton(name,value)
            else:
                raise InputError('Unsupported field/value CSV label')
        if current is not None:
            raw['items'].append(current)
    else:
        headers=[ALIASES.get(c,c) for c in columns]
        if len(set(headers))!=len(headers):
            raise InputError('Conflicting CSV column aliases')
        required={'invoice_number','vendor','due_date','item','quantity','unit_price'}
        if not required.issubset(headers):
            return None
        for cells in rows[1:]:
            row=dict(zip(headers,cells))
            if row.get('item') or row.get('invoice_number'):
                for name in METADATA & row.keys():
                    value=row[name]
                    if name in raw and raw[name]!=value:
                        raise InputError('CSV contains conflicting invoice headers')
                    raw[name]=value
                raw['items'].append({k:row[k] for k in ('item','quantity','unit_price','amount') if k in row})
            else:
                nonempty=[c.strip() for c in cells if c.strip()]
                if len(nonempty)!=2:
                    raise InputError('Ambiguous CSV footer')
                label,value=nonempty
                label=label.rstrip(':').strip()
                if re.fullmatch(r'(?:sales\s+)?tax(?:\s*\([^)]*\))?',label,re.I):
                    name='tax_amount'
                else:
                    name=ALIASES.get(key(label))
                if name not in {'subtotal','tax_amount','total','shipping_amount'}:
                    raise InputError('Unsupported CSV footer label')
                singleton(name,value)
    raw['due_date']=date_value(raw.get('due_date'))
    return raw
