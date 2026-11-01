"""strategylab.py: reconciliation, break-even flags, session-clustered statistics, policies chosen on the
first half only, read-only."""
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import strategylab as sl

ROOT = Path(__file__).resolve().parent.parent
COINS = ('BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'HYPE', 'BNB')


def trade(coin, code, close, won, entry=.85, size=10, opened_before=300, side='yes'):
    cost = round(entry * size + .1, 4)
    raw = dict(mode='shadow', ticker=f'KX{coin}15M-{code}-00', asset=coin, side=side, entry=entry, size=size, cost=cost, entry_fee=.1,
               exit=1.0 if won else 0.0, exit_fee=0, pnl=round((size if won else 0) - cost, 4), reason='settlement',
               opened=close - opened_before, closed=close, close=close, cfg='v1')
    return raw


def grouped(raws):
    import pnlreport
    ts = pnlreport.group_trades(raws)
    for t in ts:
        t['asset'] = t['raw'][0]['asset']
    return ts


class MetricTests(unittest.TestCase):
    def test_session_metrics_by_hand(self):
        ts = grouped([trade('BTC', 'S1', 1000, True), trade('ETH', 'S1', 1000, False), trade('BTC', 'S2', 2000, True)])
        m = sl.metrics(ts)
        self.assertAlmostEqual(m['net'], 1.4 - 8.6 + 1.4); self.assertAlmostEqual(m['worst_session'], 1.4 - 8.6)
        self.assertAlmostEqual(m['dd'], 7.2); self.assertAlmostEqual(m['avg_loss'], -8.6); self.assertEqual(m['sessions'], 2)

    def test_cluster_ci_needs_sessions_and_is_reproducible(self):
        few = grouped([trade('BTC', f'S{i}', 1000 * (i + 1), i % 2 == 0) for i in range(4)])
        self.assertIsNone(sl.cluster_ci(few))
        many = grouped([trade(c, f'S{i}', 1000 * (i + 1), (i + j) % 5 != 0) for i in range(30) for j, c in enumerate(COINS[:3])])
        self.assertEqual(sl.cluster_ci(many), sl.cluster_ci(many))

    def test_cluster_interval_is_wider_when_coins_lose_together(self):
        rnd = random.Random(2); together, apart = [], []
        for i in range(60):
            lose = rnd.random() < .12
            together += [trade(c, f'S{i}', 1000 * (i + 1), not lose) for c in COINS[:4]]                       # whole session wins or loses
            apart += [trade(c, f'S{i}', 1000 * (i + 1), rnd.random() >= .12) for c in COINS[:4]]              # independent coins
        w = lambda ci: ci[1] - ci[0]
        self.assertGreater(w(sl.cluster_ci(grouped(together))), w(sl.cluster_ci(grouped(apart))))

    def test_policies_are_chosen_on_the_first_half_only(self):
        train = grouped([trade('BTC' if i % 2 else 'ETH', f'A{i}', 1000 * (i + 1), i % 2 == 1) for i in range(40)])   # BTC wins, ETH loses
        test = grouped([trade('BTC' if i % 2 else 'ETH', f'B{i}', 90000 + 1000 * i, i % 2 == 0) for i in range(40)])  # reversed later
        names = {n: p for n, p, _ in sl.policies(train, test, {})}
        self.assertEqual(names['Single best coin (from first half)'], 'BTC')        # not ETH, which wins only in the test half


class ReportTests(unittest.TestCase):
    def build(self, d):
        rnd = random.Random(9); cache = Path(d) / 'cache'; cache.mkdir(); raws = []
        for s in range(60):
            close = 1793000000 + 900 * s; code = f'26NOV01{s:04d}'
            for i, coin in enumerate(COINS):
                level = .75 if i < 5 else .25                                       # 5 coins favour YES, BNB and HYPE favour NO
                cs = [dict(end_period_ts=close - 900 + 60 * k, volume_fp='10', price={}, yes_bid=dict(close_dollars=f'{level:.4f}'),
                           yes_ask=dict(close_dollars=f'{level + .01:.4f}')) for k in range(1, 16)]
                (cache / f'KX{coin}15M-{code}-00.json').write_text(json.dumps(dict(candlesticks=cs)))
            raws.append(trade('BTC', code, close, rnd.random() < .93))
            raws.append(trade('ETH', code, close, rnd.random() < .93))
            raws.append(trade('BNB', code, close, rnd.random() < .70, side='no'))  # bought NO while most other coins favour YES
            raws.append(trade('DOGE', code, close, rnd.random() < .93, entry=.94))  # expensive: 93% wins still lose money
        db = Path(d) / 'p.db'; con = sqlite3.connect(db)
        con.execute('create table state(id integer primary key, body text)')
        con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=raws, config_versions={'v1': {'settings': {'entry_max': .92}}})),))
        con.commit(); con.close()
        return db, cache

    def test_full_report(self):
        with tempfile.TemporaryDirectory() as d:
            db, cache = self.build(d); before = db.read_bytes()
            out = subprocess.run([sys.executable, str(ROOT / 'strategylab.py'), '--db', str(db), '--cache', str(cache), '--offline'],
                                 capture_output=True, text=True, timeout=300)
            self.assertEqual(db.read_bytes(), before)
        self.assertEqual(out.returncode, 0, out.stderr); o = out.stdout
        self.assertIn('| Closed trades (partial exits combined) | 240 |', o)
        self.assertIn('| Exact duplicate ledger rows | 0 |', o); self.assertIn('| equal |', o)
        self.assertIn('NEGATIVE EV despite high win rate', o)
        for name in ('Existing strategy', 'One market per session', 'Single best coin', 'Agreement filter', 'Agreement >='):
            self.assertIn(name, o)
        rows = {line.split('|')[1].strip(): line for line in o.split('## 5.')[1].split('## 6.')[0].splitlines() if line.startswith('| ')}
        n = lambda key: int(next(v for k, v in rows.items() if k.startswith(key)).split('|')[2])
        self.assertEqual(n('Existing strategy'), 120)
        self.assertEqual(n('Agreement filter'), 90)                 # the 30 test-half BNB trades against the other coins are removed
        ranking = o.split('## 4.')[1].split('## 5.')[0]
        self.assertLess(ranking.index('| BTC |') if '| BTC |' in ranking else ranking.index('| ETH |'), ranking.index('| BNB |'))


if __name__ == '__main__':
    unittest.main()
