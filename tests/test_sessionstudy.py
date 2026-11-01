"""sessionstudy.py: measurements at 10 minutes left from Kalshi candles (real response format), offline, read-only."""
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sessionstudy as ss

ROOT = Path(__file__).resolve().parent.parent
COINS = ('BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'HYPE', 'BNB')


def candle(end, yes_bid, vol=1000.0):
    d = lambda v: f'{v:.4f}'
    return dict(end_period_ts=end, open_interest_fp='1.00', volume_fp=f'{vol:.2f}',
                price=dict(close_dollars=d(yes_bid)), yes_bid=dict(close_dollars=d(yes_bid)), yes_ask=dict(close_dollars=d(yes_bid + .01)))


class MeasureTests(unittest.TestCase):
    def test_measurements_at_ten_minutes_left(self):
        close = 100000
        raw = [(close - 840 + 60 * i, b, b + .01, 10.0) for i, b in enumerate(   # candles end at open+60 .. close
            (.50, .60, .40, .70, .80, .85, .9, .9, .9, .9, .9, .9, .9, .9, .9))]
        m = ss.measure(raw, close, 'yes')
        self.assertEqual(m['agreed_at_10'], 1.0)                 # YES favoured at 10 min left
        self.assertAlmostEqual(m['drift_0_10'], .80 - .50)        # first candle to the one ending at close-600 (5th)
        self.assertEqual(m['flips_0_10'], 2.0)                    # .60 -> .40 -> .70 crosses 50c twice
        self.assertAlmostEqual(ss.measure(raw, close, 'no')['drift_0_10'], -(.80 - .50))


class StudyTests(unittest.TestCase):
    def build(self, d):
        rnd = random.Random(4); cache = Path(d) / 'cache'; cache.mkdir(); trades = []
        for s in range(40):
            close = 1793000000 + 900 * s; code = f'26NOV01{s:04d}'
            for i, coin in enumerate(COINS):
                majority_yes = i < 5                                  # 5 coins favour YES, 2 favour NO at 10 min left
                level = .75 if majority_yes else .25
                raw = dict(candlesticks=[candle(close - 900 + 60 * k, level + rnd.uniform(-.02, .02)) for k in range(1, 16)])
                (cache / f'KX{coin}15M-{code}-00.json').write_text(json.dumps(raw))
            for coin in ('BTC', 'ETH', 'DOGE', 'BNB'):                # traded coins; BNB bought YES against its own NO favourite
                won = coin != 'BNB' or rnd.random() < .3
                trades.append(dict(mode='shadow', ticker=f'KX{coin}15M-{code}-00', asset=coin, side='yes', entry=.85, size=10, cost=8.6,
                                   exit=1.0 if won else 0.0, exit_fee=0, pnl=1.4 if won else -8.6, reason='settlement',
                                   opened=close - 300, closed=close, close=close))
        db = Path(d) / 'p.db'; con = sqlite3.connect(db)
        con.execute('create table state(id integer primary key, body text)')
        con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades)),)); con.commit(); con.close()
        return db, cache

    def test_finds_the_planted_pattern_offline_and_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db, cache = self.build(d); before = db.read_bytes()
            out = subprocess.run([sys.executable, str(ROOT / 'sessionstudy.py'), '--db', str(db), '--cache', str(cache), '--offline'],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(db.read_bytes(), before)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('40 sessions, 160 settled trades', out.stdout)
        self.assertIn('Already favourite at 10 min', out.stdout)
        strongest = out.stdout.split('## 3.')[1]
        self.assertTrue('Already favourite' in strongest or 'Other coins agree' in strongest or 'Favourite strength' in strongest, strongest)

    def test_offline_without_cache_reports_no_history(self):
        with tempfile.TemporaryDirectory() as d:
            db, _ = self.build(d)
            out = subprocess.run([sys.executable, str(ROOT / 'sessionstudy.py'), '--db', str(db), '--cache', str(Path(d) / 'empty'), '--offline'],
                                 capture_output=True, text=True, timeout=120)
        self.assertIn('No trades with history', out.stdout)


if __name__ == '__main__':
    unittest.main()
