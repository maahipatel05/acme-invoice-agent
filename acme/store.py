"""SQLite: inventory, vendor master, workflow runs and the simulated payment ledger."""
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

# The brief's minimum seed (item, stock), extended with a catalog unit price so
# validation can also catch overbilling.
SEED = [('WidgetA', 15, 25000), ('WidgetB', 10, 50000), ('GadgetX', 5, 75000), ('FakeItem', 0, 100000)]
# Mock vendor master: suppliers Acme has onboarded. FastShip Ltd. is listed under
# its old name only, so an invoice from a renamed entity must be verified.
VENDORS = ['Widgets Inc.', 'Gadgets Co.', 'Precision Parts Ltd.', 'Global Supply Chain Partners',
           'Acme Industrial Supplies', 'MegaWidgets Corp', 'Consolidated Materials Group',
           'Summit Manufacturing Co.', 'Atlas Industrial Supply', 'TechParts International',
           'Reliable Components Inc.', 'FastShip Ltd.']


def mock_payment(vendor, amount):
    """The brief's mock banking API. Logs go to stderr so CLI stdout stays JSON."""
    print(f'Paid {amount} to {vendor}', file=sys.stderr)
    return {'status': 'success'}


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inventory(item TEXT PRIMARY KEY, stock INTEGER NOT NULL CHECK(stock>=0));
            CREATE TABLE IF NOT EXISTS vendors(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY, source_hash TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
              status TEXT NOT NULL, result TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS payments(
              identity TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
              vendor TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
              currency TEXT NOT NULL, reference TEXT NOT NULL UNIQUE,
              created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(inventory)')}
            if 'unit_price_cents' not in columns:
                db.execute('ALTER TABLE inventory ADD COLUMN unit_price_cents INTEGER')
            if 'identity' not in {r['name'] for r in db.execute('PRAGMA table_info(runs)')}:
                db.execute('ALTER TABLE runs ADD COLUMN identity TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS runs_identity ON runs(identity)')
            db.executemany('INSERT OR IGNORE INTO inventory(item, stock, unit_price_cents) VALUES (?, ?, ?)', SEED)
            db.executemany('UPDATE inventory SET unit_price_cents=? WHERE item=? AND unit_price_cents IS NULL',
                           [(price, item) for item, _, price in SEED])
            db.executemany('INSERT OR IGNORE INTO vendors VALUES (?)', [(v,) for v in VENDORS])

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self):
        with self.connect() as db:
            return [json.loads(r['result']) for r in db.execute('SELECT result FROM runs ORDER BY created DESC, rowid DESC')]

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT result FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return json.loads(row['result'])

    def checkpoint(self, result):
        """Persist in-flight progress so the console can show each agent step live."""
        with self.transaction() as db:
            Store.save(db, result)

    @staticmethod
    def save(db, result):
        db.execute('UPDATE runs SET status=?, result=?, identity=? WHERE id=?',
                   (result['status'], json.dumps(result), result.get('identity'), result['id']))
