"""Reversal exit and record-only mode (both off by default)."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import indicators as ind
from helpers import NOW, EngineCase, market
from settings import DEFAULTS, validate

STRIKE = 100.0
ROOT = Path(__file__).resolve().parent.parent


def ticks(price_now, seconds=10, before=101.0):
    """Price at ``before`` until ``seconds`` ago, then at ``price_now``."""
    return [(t, before if t < NOW - seconds else price_now) for t in range(NOW - 120, NOW + 1)]


class SignalTests(unittest.TestCase):
    def test_against_yes_is_below_the_target_by_the_margin(self):
        self.assertIsNotNone(ind.reversal(ticks(99.9), STRIKE, 'yes', NOW, .02, 5))
        self.assertIsNone(ind.reversal(ticks(99.99), STRIKE, 'yes', NOW, .02, 5))      # inside the margin
        self.assertIsNone(ind.reversal(ticks(100.5), STRIKE, 'yes', NOW, .02, 5))

    def test_against_no_is_above_the_target(self):
        self.assertIsNotNone(ind.reversal(ticks(100.1, before=99), STRIKE, 'no', NOW, .02, 5))
        self.assertIsNone(ind.reversal(ticks(99.5, before=99), STRIKE, 'no', NOW, .02, 5))

    def test_needs_the_whole_confirmation_window(self):
        self.assertIsNone(ind.reversal(ticks(99.9, seconds=2), STRIKE, 'yes', NOW, .02, 5))
        self.assertIsNotNone(ind.reversal(ticks(99.9, seconds=2), STRIKE, 'yes', NOW, .02, 1))

    def test_stale_or_future_prices_are_not_used(self):
        self.assertIsNone(ind.reversal([(t, 99.0) for t in range(NOW - 60, NOW - 10)], STRIKE, 'yes', NOW))
        future = ticks(101.0, before=101.0) + [(NOW + s, 90.0) for s in range(1, 20)]
        self.assertIsNone(ind.reversal(future, STRIKE, 'yes', NOW))

    def test_validation(self):
        validate(dict(DEFAULTS, assets=list(DEFAULTS['assets'])))
        self.assertFalse(DEFAULTS['reversal_exit']); self.assertFalse(DEFAULTS['reversal_watch'])
        for bad in (dict(reversal_pct=-1), dict(reversal_pct=2), dict(reversal_seconds=61), dict(reversal_seconds=1.5), dict(reversal_min_bid=0), dict(reversal_min_bid=1)):
            with self.assertRaises(ValueError, msg=bad):
                validate(dict(DEFAULTS, assets=list(DEFAULTS['assets']), **bad))


class EngineTests(EngineCase):
    def enter(self, **cfg):
        self.cfg(stop=0, take_profit=1, **cfg)
        self.spot['BTC'] = ticks(101.0)
        self.e.control('start'); self.e.tick(market('BTC', strike=STRIKE), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def reverse(self, bid=.55):
        later = NOW + 5          # five seconds after entry: the price has been below the target for the last 10 s
        self.spot['BTC'] = [(t, 101.0 if t < later - 10 else 99.8) for t in range(later - 120, later + 1)]
        self.e.tick(market('BTC', strike=STRIKE, yes_bid=bid, yes_ask=bid + .02, no_bid=.97 - bid, no_ask=1.01 - bid, fetched=NOW + 5), NOW + 5)

    def test_off_by_default_holds(self):
        self.enter(); self.reverse()
        self.assertIn('BTC', self.e.s['positions'])
        self.assertFalse([e for e in self.events() if e['event'] == 'reversal_signal'])

    def test_watch_records_once_and_never_sells(self):
        self.enter(reversal_watch=True); self.reverse(); self.reverse()
        self.assertIn('BTC', self.e.s['positions'])
        sig = [e for e in self.events() if e['event'] == 'reversal_signal']
        self.assertEqual(len(sig), 1)
        self.assertEqual((sig[0]['side'], sig[0]['bid']), ('yes', .55))

    def test_exit_sells_at_or_above_the_minimum_bid(self):
        self.enter(reversal_exit=True); self.reverse(bid=.55)
        self.assertNotIn('BTC', self.e.s['positions'])
        t = self.e.s['trades'][0]
        self.assertEqual(t['reason'], 'reversal exit'); self.assertGreaterEqual(t['exit'], .30)

    def test_exit_holds_when_the_bid_is_below_the_minimum(self):
        self.enter(reversal_exit=True); self.reverse(bid=.20)
        self.assertIn('BTC', self.e.s['positions'])
        self.assertTrue([e for e in self.events() if e['event'] == 'reversal_signal'])

    def test_no_signal_while_the_coin_stays_on_our_side(self):
        self.enter(reversal_exit=True)
        self.spot['BTC'] = ticks(100.5)
        self.e.tick(market('BTC', strike=STRIKE, yes_bid=.80, fetched=NOW + 1), NOW + 1)
        self.assertIn('BTC', self.e.s['positions'])


class ReportTests(unittest.TestCase):
    def test_report_compares_selling_with_holding(self):
        with tempfile.TemporaryDirectory() as d:
            db, log = Path(d) / 'p.db', Path(d) / 'log.jsonl'
            trades = [dict(mode='paper', ticker='KXBTC15M-A', side='yes', entry=.85, size=10, cost=8.6, exit=0.0, exit_fee=0, pnl=-8.6, reason='settlement', opened=1, closed=2),
                      dict(mode='paper', ticker='KXBTC15M-B', side='yes', entry=.85, size=10, cost=8.6, exit=1.0, exit_fee=0, pnl=1.4, reason='settlement', opened=1, closed=2)]
            con = sqlite3.connect(db); con.execute('create table state(id integer primary key, body text)')
            con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades)),)); con.commit(); con.close()
            log.write_text('\n'.join(json.dumps(dict(event='reversal_signal', mode='paper', ticker=t, side='yes', size=10, bid=.5, entry=.85)) for t in ('KXBTC15M-A', 'KXBTC15M-B')) + '\n')
            out = subprocess.run([sys.executable, str(ROOT / 'reversalreport.py'), '--db', str(db), '--log', str(log)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('better in 1 of 2', out.stdout)


if __name__ == '__main__':
    unittest.main()


class IndexOnlyTests(EngineCase):
    def make(self, **kw):
        self.index = {}
        return super().make(index_fn=lambda a: self.index.get(a, []), **kw)

    def test_a_coinbase_fallback_price_never_triggers_a_reversal(self):
        self.cfg(stop=0, take_profit=1, reversal_exit=True)
        self.spot['BTC'] = ticks(101.0); self.index['BTC'] = ticks(101.0)
        self.e.control('start'); self.e.tick(market('BTC', strike=STRIKE), NOW)
        later = NOW + 5
        self.spot['BTC'] = [(t, 101.0 if t < later - 10 else 99.8) for t in range(later - 120, later + 1)]   # Coinbase "crosses"
        self.index['BTC'] = []                                                                                 # index feed down
        self.e.tick(market('BTC', strike=STRIKE, yes_bid=.55, fetched=later), later)
        self.assertIn('BTC', self.e.s['positions'])                       # holds: no index, no reversal signal
        self.index['BTC'] = self.spot['BTC']                                # the index itself crosses
        self.e.tick(market('BTC', strike=STRIKE, yes_bid=.55, fetched=later + .5), later + .5)
        self.assertNotIn('BTC', self.e.s['positions'])
