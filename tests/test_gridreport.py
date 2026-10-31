"""gridreport.py: read-only grid of entry range x window on recorded trades."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class GridReportTests(unittest.TestCase):
    def test_runs_read_only_and_reports(self):
        from settings import DEFAULTS
        close = 1793000700 // 900 * 900; trades = []
        for i in range(120):
            c = close - 900 * (i // 2); e = .80 + (i % 10) / 100; won = i % 7 != 0
            trades.append(dict(mode='paper', ticker='KXBTC15M-25OCT311815-15', asset='BTC', side='yes', entry=e, size=10, cost=e*10,
                               pnl=((1 if won else 0) - e) * 10, exit=1.0 if won else 0.0, opened=c - 60 * (1 + i % 9), closed=c, reason='settlement'))
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'p.db'; con = sqlite3.connect(db)
            con.execute('create table state(id integer primary key, body text)')
            con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades, config=DEFAULTS)),)); con.commit(); con.close()
            before = db.read_bytes()
            out = subprocess.run([sys.executable, str(ROOT / 'gridreport.py'), '--db', str(db), '--min', '10'], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('TOP 12 BY TOTAL NET P&L', out.stdout); self.assertIn('BY TIME LEFT', out.stdout)
            self.assertEqual(db.read_bytes(), before)           # never writes


if __name__ == '__main__':
    unittest.main()
