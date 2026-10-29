import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from acme.domain import InputError, normalize
from acme.ingestion import read_document
from acme.layouts import labelled_text, legacy_csv
from acme.store import Store
from acme.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / 'data/invoices'
TEXT = 'Vendor: Widgets Inc.\nInvoice Number: T-001\nDue Date: 2026-12-01\nCurrency: USD\nWidgetA qty: 1 unit price: $250.00\nTotal: $250.00\n'


def doc(name):
    path = INVOICES / name
    return read_document(name, path.read_bytes())


class AmbiguityTests(unittest.TestCase):
    """Conflicting source values must never be silently resolved into a payable invoice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'a.db')

    def assert_rejected(self, name, payload):
        result = Workflow(self.store).process(name, payload)
        self.assertEqual(result['status'], 'rejected', result.get('findings'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM payments').fetchone()[0], 0)

    def test_repeated_text_headers(self):
        for repeated in ['Currency: EUR\n', 'Total: $999999.00\n', 'Vendor: Fake Vendor\n', 'Due: 2027-01-01\n', 'Currency: USDD\n']:
            with self.subTest(repeated=repeated):
                self.assert_rejected('a.txt', (TEXT + repeated).encode())

    def test_unparsed_or_malformed_money_on_item_rows(self):
        for row in ['WidgetA 1 $100O', 'WidgetA 1 $100 -$900', 'WidgetA 1 $100 Tax: $900', 'WidgetA 1 $100 $900']:
            with self.subTest(row=row):
                self.assert_rejected('a.txt', TEXT.replace('WidgetA qty: 1 unit price: $250.00', row).replace('$250.00\n', '$100\n').encode())

    def test_csv_duplicate_columns_ragged_rows_and_conflicting_headers(self):
        header = 'invoice_number,vendor,due_date,total,item,quantity,unit_price,currency'
        row = 'T-2,Widgets Inc.,2026-12-01,250,WidgetA,1,250,USD'
        for text in [header + ',total\n' + row + ',90000', header + '\n' + row + ',x', header + '\n' + row.rsplit(',', 1)[0],
                     'Invoice Number,Vendor,Due Date,Item,Qty,Quantity,Unit Price\nA,Widgets Inc.,2026-12-01,WidgetA,1,99,250\n',
                     header + '\n' + row + '\nT-2,Evil Vendor,2026-12-01,250,WidgetB,1,500,USD']:
            with self.subTest(text=text):
                self.assert_rejected('a.csv', text.encode())

    def test_xml_repeated_fields_and_entities(self):
        text = (ROOT / 'data/scenarios/foreign.xml').read_text().replace('EUR', 'USD')
        for old, new in [('<currency>USD</currency>', '<currency>EUR</currency><currency>USD</currency>'),
                         ('<total>250.00</total>', '<total>999999</total><total>250.00</total>'),
                         ('<quantity>1</quantity>', '<quantity>99</quantity><quantity>1</quantity>')]:
            with self.subTest(new=new):
                self.assert_rejected('a.xml', text.replace(old, new).encode())
        self.assert_rejected('a.xml', b'<!DOCTYPE x [<!ENTITY e "1">]><invoice/>')

    def test_json_duplicate_keys_and_aliases(self):
        source = json.loads((ROOT / 'data/scenarios/clean.json').read_text())
        self.assert_rejected('a.json', b'{"total": "1", "total": "2"}')
        for extra in [{'amount': '999999'}, {'items': []}]:
            self.assert_rejected('a.json', json.dumps({**source, **extra}).encode())

    def test_unreadable_inputs(self):
        for name, payload in [('a.txt', b''), ('a.exe', b'x'), ('a.pdf', b'not a pdf'), ('a.txt', b'\xff\xfe\x00bad'),
                              ('a.json', b'{"a":' * 5000 + b'1' + b'}' * 5000), ('a.txt', b'Hello, nothing to see here.')]:
            with self.subTest(name=name, payload=payload[:20]):
                result = Workflow(self.store).process(name, payload)
                self.assertEqual(result['status'], 'rejected')


class LayoutTests(unittest.TestCase):
    def test_all_three_pdfs_parse_identically_to_their_text_twins(self):
        for number in ('1011', '1012', '1013'):
            with self.subTest(number=number):
                pdf, _ = normalize(doc(f'invoice_{number}.pdf')['raw'])
                twin = f'invoice_{number}.txt' if number != '1013' else 'invoice_1013.json'
                other, _ = normalize(doc(twin)['raw'])
                for field in ('invoice_number', 'vendor', 'due_date', 'total_cents', 'tax_cents'):
                    self.assertEqual(pdf[field], other[field], field)
                self.assertEqual([(l['item'].replace(' ', ''), l['quantity'], l['unit_price_cents']) for l in pdf['items']],
                                 [(l['item'].replace(' ', ''), l['quantity'], l['unit_price_cents']) for l in other['items']])

    def test_pdf_without_pymupdf_falls_back_or_explains(self):
        with patch.dict(sys.modules, {'pymupdf': None}):
            if sys.platform == 'darwin':
                invoice, _ = normalize(doc('invoice_1011.pdf')['raw'])
                self.assertEqual(invoice['vendor'], 'Summit Manufacturing Co.')
            else:
                with self.assertRaisesRegex(InputError, 'pip install'):
                    doc('invoice_1011.pdf')

    def test_explicit_shipping_and_rush_order_note(self):
        invoice, findings = normalize(doc('invoice_1010.txt')['raw'])
        self.assertEqual((invoice['shipping_cents'], invoice['total_cents']), (15000, 718500))
        self.assertEqual(invoice['items'][-1]['description'], 'rush order')
        self.assertNotIn('TOTAL_MISMATCH', [f['code'] for f in findings])

    def test_field_value_and_footer_csv_layouts(self):
        raw = legacy_csv((INVOICES / 'invoice_1006.csv').read_text())
        self.assertEqual(len(raw['items']), 2)
        with self.assertRaises(InputError):
            legacy_csv((INVOICES / 'invoice_1006.csv').read_text() + 'total,1.00\n')
        raw = legacy_csv((INVOICES / 'invoice_1007.csv').read_text())
        self.assertEqual((raw['total'], raw['tax_amount']), ('15525.00', '885.00'))   # source kept, never repaired

    def test_email_sender_is_not_the_vendor(self):
        raw = labelled_text((INVOICES / 'invoice_1008.txt').read_text())
        self.assertEqual(raw['vendor'], 'NoProd Industries')
        self.assertEqual([i['item'] for i in raw['items']], ['SuperGizmo', 'MegaSprocket'])

    def test_ocr_damage_is_kept_and_flagged(self):
        raw = labelled_text((INVOICES / 'invoice_1012.txt').read_text())
        self.assertEqual((raw['items'][1]['source_amount'], raw['items'][1]['amount']), ('3,500.O0', '3,500.00'))
        self.assertTrue(raw['uncertainties'])
        self.assertEqual(raw['due_date'], '2026-02-25')

    def test_unknown_multiword_product_is_not_matched_to_a_known_suffix(self):
        raw = labelled_text(TEXT.replace('WidgetA qty: 1', 'Fake WidgetA qty: 1'))
        self.assertEqual(raw['items'][0]['item'], 'Fake WidgetA')

    def test_ambiguous_slash_dates_are_not_guessed(self):
        with self.assertRaises(InputError):
            labelled_text(TEXT.replace('2026-12-01', '03/04/2026'))
        self.assertEqual(labelled_text(TEXT.replace('2026-12-01', '02/28/2026'))['due_date'], '2026-02-28')


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run([sys.executable, str(ROOT / 'main.py'), '--db', f'{folder}/a.db', *args],
                                  capture_output=True, text=True, cwd=ROOT, timeout=120)

    def test_brief_command_works_as_written(self):
        done = self.run_cli('--invoice_path=data/invoices/invoice1.txt')
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result['invoice']['invoice_number'], result['status']), ('INV-1001', 'paid'))
        self.assertIn('Paid USD 5,000.00 to Widgets Inc.', done.stderr)       # the brief's mock_payment output
        self.assertIn('"agent": "validation"', done.stderr)                   # structured step logs

    def test_batch_summary(self):
        done = self.run_cli('--batch', 'data/invoices', '--quiet')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('2 duplicate, 5 paid, 4 pending_review, 9 rejected  (20 files', done.stdout)
