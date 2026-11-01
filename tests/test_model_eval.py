"""model_eval.py: metrics, calibration, walk-forward without look-ahead, read-only."""
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import model_eval as me

ROOT = Path(__file__).resolve().parent.parent
ET = timezone(timedelta(hours=-4))


def synthetic(n=400, edge=.07, seed=1):
    """Trades on 15-minute BTC/ETH markets; the side bought really wins with probability price + edge."""
    rnd = random.Random(seed); out = []; start = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        close = start + timedelta(minutes=15 * (i + 1)); asset = 'BTC' if i % 2 else 'ETH'
        tk = f'KX{asset}15M-{close.astimezone(ET).strftime("%y%b%d%H%M").upper()}-{close.astimezone(ET).minute:02d}'
        price = rnd.choice([.80, .83, .86, .89, .91]); won = rnd.random() < min(.99, price + edge); size = 20
        cost = price * size + .25
        out.append(dict(mode='shadow', ticker=tk, asset=asset, side=rnd.choice(['yes', 'no']), entry=price, size=size, cost=cost, entry_fee=.25,
                        exit=1.0 if won else 0.0, exit_fee=0, pnl=(size if won else 0) - cost, reason='settlement',
                        opened=close.timestamp() - 300, closed=close.timestamp()))
    return out


def db_with(trades, d):
    db = Path(d) / 'p.db'; con = sqlite3.connect(db)
    con.execute('create table state(id integer primary key, body text)')
    con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades, config={}, cash=1000, positions={})),)); con.commit(); con.close()
    return db


class MetricTests(unittest.TestCase):
    def test_profit_factor_and_drawdown_by_hand(self):
        self.assertAlmostEqual(me.profit_factor([3, 2, -4, 0]), 5 / 4)
        self.assertIsNone(me.profit_factor([1, 2]))
        self.assertEqual(me.max_drawdown([5, -3, -4, 2]), 7)

    def test_wilson_and_scores(self):
        lo, hi = me.wilson(90, 100); self.assertTrue(.82 < lo < .9 < hi < .95)
        self.assertAlmostEqual(me.brier([.8, .2], [1, 0]), .04)
        self.assertLess(me.ece([.9] * 10, [1] * 9 + [0]), 1e-9)

    def test_bootstrap_is_reproducible(self):
        ts = [dict(pnl=x, size=1) for x in (1, -2, 3, 1, 1, -1, 2)]
        self.assertEqual(me.bootstrap(ts, me.per_contract), me.bootstrap(ts, me.per_contract))


class WalkForwardTests(unittest.TestCase):
    def test_no_future_data(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [(t, 1 if t['pnl'] > 0 else 0) for t in me.load(str(db_with(synthetic(), d)), 'shadow', 0)]
        coins = ['ETH']
        a = me.walk_forward(rows, coins)
        flipped = rows[:320] + [(t, 1 - y) for t, y in rows[320:]]        # change only the last block's outcomes
        b = me.walk_forward(flipped, coins)
        self.assertEqual(a[:320], b[:320])                                 # earlier predictions cannot see them
        self.assertTrue(all(p is None for p in a[:160]))                   # nothing scored without 100+ earlier trades


class ReportTests(unittest.TestCase):
    def run_report(self, trades, *args):
        with tempfile.TemporaryDirectory() as d:
            db = db_with(trades, d); before = db.read_bytes()
            out = subprocess.run([sys.executable, str(ROOT / 'model_eval.py'), '--db', str(db), *args], capture_output=True, text=True, timeout=300)
            self.assertEqual(db.read_bytes(), before)                      # read-only
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_detects_a_real_edge_on_favourites(self):
        out = self.run_report(synthetic(edge=.07))
        self.assertIn('**positive with 95% confidence**', out)
        self.assertIn('Out-of-sample trades:', out)
        self.assertIn('## 4. Loss attribution', out)

    def test_no_edge_is_not_claimed(self):
        out = self.run_report(synthetic(edge=-.02, seed=3))
        self.assertIn('**not established**', out)

    def test_empty_and_small_samples(self):
        self.assertIn('No closed trades', self.run_report([]))
        self.assertIn('Skipped:', self.run_report(synthetic(n=60)))


if __name__ == '__main__':
    unittest.main()
