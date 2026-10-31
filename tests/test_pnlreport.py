"""P&L report: metrics, partial-exit grouping, periods, data-quality exclusions, read-only access."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pnlreport

NOW = 1793500000


def trade(ticker, pnl, mode='shadow', entry=.80, size=25, opened=NOW - 3000, closed=NOW - 2000, reason='settlement', **kw):
    cost = round(entry * size + .1, 6)
    exit_px = round((pnl + cost) / size, 6) if reason != 'settlement' else (1.0 if pnl > 0 else 0.0)
    if reason == 'settlement':
        pnl = round(exit_px * size - cost, 6)
    return dict(dict(mode=mode, ticker=ticker, asset=ticker[2:].split('15M')[0], side='yes', entry=entry, size=size, cost=cost,
                     exit=exit_px, exit_fee=0, pnl=pnl, reason=reason, opened=opened, closed=closed), **kw)


def state(trades, **kw):
    return dict(dict(trades=trades, positions={}, shadow_positions={}, cash=1000, curve=[], live_balance=None), **kw)


class MetricTests(unittest.TestCase):
    def test_win_rate_expectancy_profit_factor(self):
        rows = [trade('KXBTC15M-26OCT311530-30', 1, opened=NOW - 3000 + i) for i in range(3)] + [trade('KXETH15M-26OCT311530-30', -1)]
        m = pnlreport.metrics(pnlreport.group_trades(rows))
        self.assertEqual((m['n'], m['wins'], m['losses']), (4, 3, 1))
        self.assertAlmostEqual(m['win_rate'], .75)
        self.assertAlmostEqual(m['pnl'], 3 * 4.9 - 20.1)
        self.assertAlmostEqual(m['expectancy'], .75 * 4.9 - .25 * 20.1)
        self.assertAlmostEqual(m['profit_factor'], 3 * 4.9 / 20.1)

    def test_partial_exits_are_one_trade(self):
        a = trade('KXBTC15M-26OCT311530-30', 2.0, reason='take profit', size=10)
        b = dict(a, size=15, pnl=3.0, closed=NOW - 1500)
        g = pnlreport.group_trades([a, b])
        self.assertEqual(len(g), 1); self.assertEqual((g[0]['size'], g[0]['pnl'], g[0]['parts']), (25, 5.0, 2))

    def test_no_trades_reports_na(self):
        text = pnlreport.report(state([]), 'shadow', 'all', NOW)
        self.assertIn('every metric is N/A', text)

    def test_drawdown(self):
        peak, dd, pct = pnlreport.drawdown([(0, 1000), (1, 1100), (2, 990), (3, 1050)])
        self.assertEqual((peak, dd), (1100, 110)); self.assertAlmostEqual(pct, .1)


class ReportTests(unittest.TestCase):
    def test_only_the_requested_ledger_and_period_are_counted(self):
        rows = [trade('KXBTC15M-26OCT311530-30', 1), trade('KXETH15M-26OCT311530-30', 1, mode='paper'),
                trade('KXSOL15M-26OCT311530-30', -1, closed=NOW - 10 * 86400, opened=NOW - 10 * 86400 - 600)]
        text = pnlreport.report(state(rows), 'shadow', '7d', NOW)
        self.assertIn('**Closed trades:** 1', text)
        self.assertIn('last 7 days', text)

    def test_bad_records_are_excluded_and_listed(self):
        rows = [trade('KXBTC15M-26OCT311530-30', 1), dict(trade('KXETH15M-26OCT311530-30', 1), pnl=99.0)]
        text = pnlreport.report(state(rows), 'shadow', 'all', NOW)
        self.assertIn('**Closed trades:** 1', text)
        self.assertIn('Records excluded from calculations: 1', text); self.assertIn('does not reconcile', text)

    def test_open_positions_are_not_counted_as_trades(self):
        s = state([trade('KXBTC15M-26OCT311530-30', 1)],
                  shadow_positions=dict(ETH=dict(mode='shadow', ticker='KXETH15M-X', side='yes', entry=.8, size=10, cost=8.1, mark=.7)))
        text = pnlreport.report(s, 'shadow', 'all', NOW)
        self.assertIn('**Closed trades:** 1', text); self.assertIn('**Open positions:** 1 (unrealized -1.10', text)

    def test_comparison_counts_missed_fills_only_while_both_ran(self):
        rows = [trade('KXBTC15M-26OCT301530-30', 1, mode='paper', opened=NOW - 90000, closed=NOW - 89000),   # before shadow existed
                trade('KXBTC15M-26OCT311530-30', 1, mode='paper', entry=.81), trade('KXBTC15M-26OCT311530-30', 1, entry=.80),
                trade('KXETH15M-26OCT311530-30', 1, mode='paper')]                                                  # shadow missed it
        c = pnlreport.comparison(state(rows), 'shadow', 'paper')
        self.assertEqual((c['n'], c['only_other'], c['better']), (1, 1, 1))

    def test_database_is_opened_read_only(self):
        d = tempfile.mkdtemp(); db = str(Path(d) / 's.db')
        con = sqlite3.connect(db); con.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        con.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(state([])),)); con.commit(); con.close()
        self.assertEqual(pnlreport.load_state(db)['trades'], [])
        ro = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        with self.assertRaises(sqlite3.OperationalError): ro.execute('DELETE FROM state')
