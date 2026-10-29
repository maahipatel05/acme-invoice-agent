import unittest
from acme.domain import BLOCK, HOLD, INFO, InputError, floor_decision, money, normalize


def raw(**changes):
    base = {'invoice_number': 'T-1', 'vendor': 'Widgets Inc.', 'due_date': '2026-12-01', 'currency': 'USD',
            'items': [{'item': 'WidgetA', 'quantity': 2, 'unit_price': '250.00'}], 'total': '500.00'}
    base.update(changes)
    return base


def codes(findings):
    return [f['code'] for f in findings]


class MoneyTests(unittest.TestCase):
    def test_strict_money(self):
        self.assertEqual(money('$1,234.50'), 123450)
        self.assertEqual(money(250.1), 25010)
        self.assertEqual(money('-250'), -25000)
        for bad in ['1,2.00', '$1$0.00', '100O', '1.234', '', None, True, '1e5', 'NaN']:
            with self.subTest(bad=bad), self.assertRaises(InputError):
                money(bad)


class NormalizeTests(unittest.TestCase):
    def test_clean_invoice_has_no_blocking_findings(self):
        invoice, findings = normalize(raw())
        self.assertEqual(invoice['total_cents'], 50000)
        self.assertEqual(floor_decision(findings), 'approve')

    def test_every_problem_is_reported_not_just_the_first(self):
        # INV-1009 style: no vendor, no due date, negative quantity, negative total.
        invoice, findings = normalize(raw(vendor={'name': ''}, due_date=None, total='-250.00', subtotal='1000.00',
            items=[{'item': 'WidgetA', 'quantity': -5, 'unit_price': 250}, {'item': 'WidgetB', 'quantity': 2, 'unit_price': 500}]))
        self.assertEqual(set(codes(findings)) - {'CURRENCY_ASSUMED_USD'},
                         {'MISSING_VENDOR', 'MISSING_DUE_DATE', 'INVALID_QUANTITY', 'INVALID_AMOUNT', 'SUBTOTAL_MISMATCH'})
        self.assertEqual(invoice['items'][0]['quantity'], -5)   # kept for the operator to see

    def test_total_mismatch_explains_the_arithmetic(self):
        _, findings = normalize(raw(total='600.00', tax_amount='50.00'))
        message = next(f['message'] for f in findings if f['code'] == 'TOTAL_MISMATCH')
        self.assertIn('lines 500.00 + tax 50.00 = 550.00 (off by 50.00)', message)

    def test_tax_and_shipping_reconcile_and_are_never_inferred(self):
        _, ok = normalize(raw(total='575.00', tax_amount='25.00', shipping_amount='50.00'))
        self.assertEqual(floor_decision(ok), 'approve')
        _, missing = normalize(raw(total='575.00', tax_amount='25.00'))
        self.assertIn('TOTAL_MISMATCH', codes(missing))

    def test_line_amount_must_match_quantity_times_price(self):
        _, findings = normalize(raw(items=[{'item': 'WidgetA', 'quantity': 2, 'unit_price': '250', 'amount': '900'}]))
        self.assertIn('LINE_AMOUNT_MISMATCH', codes(findings))

    def test_quantities_must_be_positive_whole_numbers(self):
        for quantity in [0, -1, '1.5', 'two', True, None]:
            with self.subTest(quantity=quantity):
                _, findings = normalize(raw(items=[{'item': 'WidgetA', 'quantity': quantity, 'unit_price': '250'}], total='250'))
                self.assertIn('INVALID_QUANTITY', codes(findings))

    def test_relative_and_invalid_due_dates(self):
        _, relative = normalize(raw(due_date='yesterday'))
        self.assertIn('RELATIVE_DUE_DATE', codes(relative))
        _, invalid = normalize(raw(due_date='2026-02-30'))
        self.assertIn('INVALID_DUE_DATE', codes(invalid))

    def test_missing_currency_is_assumed_usd_and_recorded(self):
        invoice, findings = normalize(raw(currency=None))
        self.assertEqual(invoice['currency'], 'USD')
        self.assertTrue(invoice['currency_assumed'])
        self.assertEqual([(f['code'], f['severity']) for f in findings], [('CURRENCY_ASSUMED_USD', INFO)])
        invoice, _ = normalize(raw(currency='usd'))
        self.assertEqual(invoice['currency'], 'USD')
        _, odd = normalize(raw(currency='US Dollars'))
        self.assertEqual([f['severity'] for f in odd if f['code'] == 'INVALID_CURRENCY'], [HOLD])

    def test_extraction_uncertainty_holds(self):
        _, findings = normalize(raw(uncertainties=['OCR: 3,500.O0']))
        self.assertEqual(floor_decision(findings), 'review')

    def test_conflicting_aliases_are_unreadable(self):
        for bad in [raw(amount='1'), raw(line_items=[]), raw(items=[{'item': 'A', 'name': 'B', 'quantity': 1, 'unit_price': 1}])]:
            with self.subTest(bad=bad), self.assertRaises(InputError):
                normalize(bad)

    def test_floor(self):
        self.assertEqual(floor_decision([{'severity': BLOCK}, {'severity': HOLD}]), 'reject')
        self.assertEqual(floor_decision([{'severity': INFO}]), 'approve')
