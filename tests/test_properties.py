"""Randomized safety properties: whatever an invoice contains, money only moves when every rule holds.

Seeded, so failures reproduce. Each case mutates a valid invoice the way real documents go wrong
(typos, wrong totals, odd quantities, unknown items, duplicate submissions) and checks invariants
on the outcome rather than a hand-picked expected answer.
"""
import json
import random
import tempfile
import unittest
from pathlib import Path
from acme.domain import money
from acme.store import Store
from acme.workflow import Workflow

ITEMS = ['WidgetA', 'WidgetB', 'GadgetX', 'FakeItem', 'WidgetC', 'widget a', 'GADGET-X', '']
VENDORS = ['Widgets Inc.', 'WIDGETS INC', 'Widgets lnc.', 'Fraudster LLC', '', 'Gadgets Co.']
PRICES = ['250.00', '500', '750.00', '1,000.00', '-5', '0', '12.345', '1e3', '$300', 'abc', 250, 500.0]
QUANTITIES = [1, 2, 5, 9, 15, 16, -3, 0, 2.5, '3', 'x', True, None]
STATUSES = {'paid', 'pending_review', 'rejected', 'duplicate'}


def mutate(rng, n):
    """A valid invoice, with each field going wrong now and then the way real documents do."""
    def maybe(good, bad, p=0.12):
        return rng.choice(bad) if rng.random() < p else good
    lines = [{'item': maybe(rng.choice(['WidgetA', 'WidgetB', 'GadgetX']), ITEMS), 'quantity': maybe(rng.randint(1, 3), QUANTITIES),
              'unit_price': maybe({'WidgetA': '250.00', 'WidgetB': '500.00', 'GadgetX': '750.00'}.get('WidgetA'), PRICES)}
             for _ in range(rng.randint(1, 3))]
    try:
        true_total = sum(int(l['quantity']) * money(l['unit_price']) for l in lines) / 100
    except (TypeError, ValueError):
        true_total = 100
    return {'invoice_number': maybe(f'P-{rng.randint(1, 60)}', ['', None]),      # small pool -> real duplicates
            'vendor': maybe(rng.choice(['Widgets Inc.', 'Gadgets Co.', 'Precision Parts Ltd.']), VENDORS),
            'due_date': maybe('2026-12-01', ['yesterday', '2026-02-30', None, '12/01/2026']),
            'currency': maybe('USD', [None, 'EUR', 'usd', 'dollars']), 'items': lines,
            'total': maybe(f'{true_total:.2f}', [f'{true_total + 10:.2f}', '-1', 'n/a'])}


class PropertyTests(unittest.TestCase):
    def test_money_only_moves_when_every_rule_holds(self):
        rng = random.Random(20261028)
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'p.db')
            outcomes = []
            for n in range(400):
                body = mutate(rng, n)
                result = Workflow(store).process(f'{n}.json', json.dumps(body).encode())
                outcomes.append(result['status'])
                with self.subTest(n=n, body=body):
                    self.assertIn(result['status'], STATUSES)                    # never crashes, never "error" offline
                    blocking = [f for f in result['findings'] if f['severity'] in ('block', 'hold')]
                    if result['status'] == 'paid':
                        inv = result['invoice']
                        self.assertEqual(blocking, [])
                        self.assertEqual(inv['total_cents'], sum(l['quantity'] * l['unit_price_cents'] for l in inv['items'])
                                         + inv['tax_cents'] + inv['shipping_cents'])
                        self.assertTrue(all(l['quantity'] > 0 and l['unit_price_cents'] > 0 for l in inv['items']))
                        self.assertLessEqual(inv['total_cents'], 1_000_000)
                        self.assertEqual(inv['currency'], 'USD')
                    elif result['status'] == 'rejected':
                        self.assertTrue(any(f['severity'] == 'block' for f in result['findings']))
                    if result['status'] != 'paid':
                        self.assertNotIn('payment', result)
            with store.connect() as db:
                ledger = db.execute('SELECT identity, count(*) AS n FROM payments GROUP BY identity').fetchall()
                stock = {r['item']: r['stock'] for r in db.execute('SELECT * FROM inventory')}
            self.assertTrue(all(r['n'] == 1 for r in ledger))                     # no invoice paid twice
            self.assertEqual(stock, {'WidgetA': 15, 'WidgetB': 10, 'GadgetX': 5, 'FakeItem': 0})
            for status in STATUSES:                                               # the generator exercises every path
                self.assertIn(status, outcomes)

    def test_every_text_mutation_of_a_clean_invoice_is_either_identical_or_not_paid(self):
        clean = ('Vendor: Widgets Inc.\nInvoice Number: T-{n}\nDue Date: 2026-12-01\nCurrency: USD\n'
                 'Items:\n  WidgetA qty: 2 unit price: $250.00\nTotal: $500.00\n')
        rng = random.Random(7)
        swaps = [('2026-12-01', 'tomorrow'), ('qty: 2', 'qty: 20'), ('$250.00', '$25O.00'), ('$500.00', '$5,00.00'),
                 ('WidgetA', 'FakeItem'), ('Widgets Inc.', 'Widgets Inc'), ('USD', 'EUR'), ('Total', 'Total: $1\nTotal'),
                 ('qty: 2', 'qty: -2'), ('$500.00', '$550.00')]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 't.db')
            for n in range(120):
                old, new = rng.choice(swaps)
                text = clean.format(n=n).replace(old, new, 1)
                result = Workflow(store).process(f'{n}.txt', text.encode())
                harmless = new in ('Widgets Inc',)                                # punctuation-only vendor change
                with self.subTest(n=n, swap=(old, new)):
                    if not harmless:
                        self.assertNotEqual(result['status'], 'paid', result['findings'])
