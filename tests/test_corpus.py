"""End to end over the 20 supplied invoices plus the added scenarios, scored against evals/golden.json."""
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evals'))
import run as evals  # noqa: E402


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = evals.run('offline', progress=False)
        cls.by_file = {r['file']: r for group in cls.rows.values() for r in group}

    def test_every_outcome_and_expected_finding(self):
        bad = [(r['file'], r['expected'], r['actual'], r['codes_missing']) for r in self.by_file.values()
               if not r['status_ok'] or r['codes_missing']]
        self.assertEqual(bad, [])

    def test_extraction_fields_match_hand_transcribed_values(self):
        wrong = {r['file']: r['fields_wrong'] for r in self.rows['official'] if r['fields_wrong']}
        self.assertEqual(wrong, {})
        self.assertGreater(sum(r['fields_total'] for r in self.rows['official']), 130)

    def test_supplied_files_are_unmodified(self):
        self.assertTrue(all(r['source_unchanged'] for r in self.rows['official']))
        for name, expected in evals.GOLDEN['official'].items():
            self.assertEqual(hashlib.sha256((ROOT / 'data/invoices' / name).read_bytes()).hexdigest(), expected['sha256'])

    def test_brief_scenario_table(self):
        """The README of the case lists what must happen to these invoices."""
        expectations = {'invoice_1001.txt': 'paid', 'invoice_1004.json': 'paid', 'invoice_1006.csv': 'paid',
                        'invoice_1002.txt': 'STOCK_EXCEEDED', 'invoice_1003.txt': 'ZERO_STOCK_ITEM',
                        'invoice_1008.txt': 'UNKNOWN_ITEM', 'invoice_1016.json': 'UNKNOWN_ITEM',
                        'invoice_1009.json': 'INVALID_QUANTITY'}
        for name, expected in expectations.items():
            row = self.by_file[name]
            with self.subTest(name=name):
                if expected == 'paid':
                    self.assertEqual(row['actual'], 'paid')
                else:
                    self.assertEqual(row['actual'], 'rejected')
                    self.assertIn(expected, row['codes'])

    def test_summary_counts(self):
        statuses = [r['actual'] for r in self.rows['official']]
        self.assertEqual({s: statuses.count(s) for s in set(statuses)},
                         {'paid': 5, 'duplicate': 2, 'rejected': 9, 'pending_review': 4})
