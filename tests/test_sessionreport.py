"""sessionreport.py: read-only per-session loss pattern report."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SessionReportTests(unittest.TestCase):
    def test_flags_trades_against_the_majority_and_never_writes(self):
        from settings import DEFAULTS
        trades = []
        for s in range(10):
            for i, coin in enumerate(('BTC', 'ETH', 'SOL')):
                side, won = ('no', False) if i == 2 else ('yes', True)          # SOL always against the others, always loses
                trades.append(dict(mode='paper', ticker=f'KX{coin}15M-26NOV01{s:04d}-00', asset=coin, side=side, entry=.85, size=10, cost=8.6,
                                   exit=1.0 if won else 0.0, exit_fee=0, pnl=1.4 if won else -8.6, reason='settlement', opened=1793500000 + s * 900, closed=1793500300 + s * 900))
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'p.db'; con = sqlite3.connect(db)
            con.execute('create table state(id integer primary key, body text)')
            con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades, config=DEFAULTS, cash=1000, positions={})),)); con.commit(); con.close()
            before = db.read_bytes()
            out = subprocess.run([sys.executable, str(ROOT / 'sessionreport.py'), '--db', str(db), '--since', '2026-10-01'], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            line = next(x for x in out.stdout.splitlines() if 'AGAINST majority' in x)
            self.assertIn('win   0.0%', line)
            self.assertIn('sessions with at least one loss: 10 of 10', out.stdout)
            self.assertEqual(db.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
