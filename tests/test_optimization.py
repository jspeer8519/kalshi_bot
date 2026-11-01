"""Settings labels on trades, the majority direction filter and the per-session risk cap (both off by default)."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CLOSE, NOW, EngineCase, market
from settings import DEFAULTS, settings_tag, validate

ROOT = Path(__file__).resolve().parent.parent
NO_FAVORED = dict(yes_ask=.17, yes_bid=.15, no_ask=.85, no_bid=.83)


class SettingsTagTests(EngineCase):
    def test_trades_carry_the_settings_label_and_the_settings_are_kept(self):
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        p = self.e.s['positions']['BTC']
        self.assertEqual(p['cfg'], settings_tag(self.e.s['config']))
        self.assertEqual(self.e.s['config_versions'][p['cfg']]['settings']['entry_max'], self.e.s['config']['entry_max'])
        self.e.tick(market('BTC', status='finalized', result='yes', fetched=CLOSE + 5), CLOSE + 5)
        self.assertEqual(self.e.s['trades'][0]['cfg'], p['cfg'])          # the closed trade keeps the label

    def test_label_changes_only_with_trading_settings(self):
        c = dict(DEFAULTS)
        self.assertEqual(settings_tag(c), settings_tag(dict(c, audit_full=True, final_minute_log=True, reversal_watch=True)))
        self.assertNotEqual(settings_tag(c), settings_tag(dict(c, entry_max=.90)))


class MajorityFilterTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(max_same_side=0)

    def others(self, *fav):
        for a, f in zip(('ETH', 'SOL', 'DOGE'), fav, strict=False):   # coins in the default selection
            self.e.tick(market(a, **(NO_FAVORED if f == 'no' else {})), NOW)

    def test_off_by_default_enters_against_the_majority(self):
        self.others('no', 'no'); self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_skips_when_most_others_favor_the_other_side(self):
        self.cfg(direction_majority=True); self.others('no', 'no', 'yes')
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('1 agree, 2 against YES', self.e.snapshot()['blocked']['BTC'])

    def test_skips_a_tie(self):
        self.cfg(direction_majority=True); self.others('no', 'yes')
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])

    def test_enters_with_the_majority_even_if_one_disagrees(self):
        self.cfg(direction_majority=True); self.others('yes', 'yes', 'no')
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_no_other_market_data_skips(self):
        self.cfg(direction_majority=True); self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])


DEEP = dict(yes_ask_size=100, no_ask_size=100)     # enough contracts on the book for a $20 order


class SessionRiskTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(max_same_side=0, size_mode='dollars', dollars=20)

    def test_off_by_default(self):
        self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL'): self.e.tick(market(a, **DEEP), NOW)
        self.assertEqual(len(self.e.s['positions']), 3)

    def test_cap_limits_one_session_and_resets_for_the_next(self):
        self.cfg(max_session_risk=45); self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL'): self.e.tick(market(a, **DEEP), NOW)
        self.assertEqual(sorted(self.e.s['positions']), ['BTC', 'ETH'])                 # 2 x ~$20 fit, the third would not
        self.assertIn('Session cap', self.e.snapshot()['blocked']['SOL'])
        self.e.tick(market('SOL', ticker='KXSOL15M-T2', close=CLOSE + 900, fetched=NOW + 800, **DEEP), NOW + 800)
        self.assertIn('SOL', self.e.s['positions'])                                    # a new session has its own budget

    def test_validation(self):
        validate(dict(DEFAULTS, assets=list(DEFAULTS['assets'])))
        self.assertEqual((DEFAULTS['direction_majority'], DEFAULTS['max_session_risk']), (False, 0))
        for bad in (-1, 10001):
            with self.assertRaises(ValueError):
                validate(dict(DEFAULTS, assets=list(DEFAULTS['assets']), max_session_risk=bad))


class ReportTests(unittest.TestCase):
    def test_sessionreport_shows_versions_and_hours(self):
        trades = [dict(mode='paper', ticker=f'KX{a}15M-26NOV010230-30', asset=a, side='yes', entry=.85, size=10, cost=8.6, exit=1.0, exit_fee=0,
                       pnl=1.4, reason='settlement', opened=1793500000, closed=1793500300, **({'cfg': 'abcd1234'} if a == 'BTC' else {})) for a in ('BTC', 'ETH')]
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'p.db'; con = sqlite3.connect(db)
            con.execute('create table state(id integer primary key, body text)')
            con.execute('insert into state values (1, ?)', (json.dumps(dict(trades=trades, config_versions={'abcd1234': {'settings': dict(DEFAULTS)}})),)); con.commit(); con.close()
            out = subprocess.run([sys.executable, str(ROOT / 'sessionreport.py'), '--db', str(db)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('abcd1234', out.stdout); self.assertIn('untagged', out.stdout); self.assertIn('by hour of entry', out.stdout)


if __name__ == '__main__':
    unittest.main()
