"""PAPER and SHADOW side by side: two ledgers per market, the same decisions, separate fills."""
import json
import sqlite3
from unittest.mock import patch

import strategy
from engine import DEFAULTS, Engine
from helpers import NOW, EngineCase, market


class DualLedgerTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(dual_shadow=True, max_same_side=0)

    def test_off_by_default(self):
        self.assertIs(DEFAULTS['dual_shadow'], False)
        self.cfg(dual_shadow=False); self.e.control('start'); self.e.tick(market(), NOW)
        self.assertIn('BTC', self.e.s['positions']); self.assertEqual(self.e.s['shadow_positions'], {})

    def test_both_ledgers_enter_the_same_market(self):
        self.e.control('start'); self.e.tick(market(), NOW)
        p, sp = self.e.s['positions']['BTC'], self.e.s['shadow_positions']['BTC']
        self.assertEqual((p['mode'], sp['mode']), ('paper', 'shadow'))
        self.assertEqual((p['side'], p['entry']), (sp['side'], sp['entry']))
        self.assertAlmostEqual(self.e.s['cash'], 999.14)                         # only paper spends paper cash
        self.assertIn('KXBTC15M-T1', self.e.s['seen']); self.assertIn('KXBTC15M-T1', self.e.s['shadow_seen'])
        snap = self.e.snapshot()
        self.assertIn('BTC', snap['shadow_positions']); self.assertEqual(snap['equity_by_mode']['shadow'], 1000 + (.83 - .86))

    def test_fills_can_differ_and_each_ledger_exits_on_its_own(self):
        self.cfg(contracts=3, shadow_latency_ms=1000, stop_seconds=0)
        self.e.control('start'); self.e.tick(market(), NOW)                       # paper fills now, shadow in flight
        self.assertIn('BTC', self.e.s['positions']); self.assertNotIn('BTC', self.e.s['shadow_positions'])
        self.e.tick(market(no_bids=[[.16, 2.5]], fetched=NOW+1), NOW+1)          # shadow arrives: 2 whole contracts at 84c
        self.assertEqual((self.e.s['shadow_positions']['BTC']['size'], self.e.s['shadow_positions']['BTC']['entry']), (2, .84))
        self.e.tick(market(yes_bid=.30, fetched=NOW+2), NOW+2)                   # stop: paper exits now, shadow order in flight
        self.assertNotIn('BTC', self.e.s['positions']); self.assertIn('BTC', self.e.s['shadow_positions'])
        self.e.tick(market(yes_bid=.25, yes_bids=[[.25, 10]], fetched=NOW+3), NOW+3)
        self.assertNotIn('BTC', self.e.s['shadow_positions'])
        by_mode = {t['mode']: t for t in self.e.s['trades']}
        self.assertEqual((by_mode['paper']['exit'], by_mode['shadow']['exit']), (.30, .25))

    def test_missed_shadow_fill_does_not_retry_that_market(self):
        self.cfg(shadow_latency_ms=1000)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.e.tick(market(yes_ask=.90, no_bid=.10, no_bids=[[.10, 10]], fetched=NOW+1), NOW+1)   # ask ran past the limit
        self.e.tick(market(fetched=NOW+2), NOW+2)
        self.assertNotIn('BTC', self.e.s['shadow_positions']); self.assertIn('BTC', self.e.s['positions'])

    def test_each_ledger_makes_the_same_decisions_as_running_alone(self):
        def decisions(dual):
            e = Engine(self.path + str(dual)); e.s['config'].update(dual_shadow=dual, max_same_side=0)
            seen = []
            real = strategy.decide_entry
            def spy(asset, m, x):
                d = real(asset, m, x); seen.append((x.label, asset, d and (type(d).__name__, getattr(d, 'side', None), getattr(d, 'count', None)))); return d
            e.control('start')
            with patch.object(strategy, 'decide_entry', spy):
                for a in ('BTC', 'ETH'): e.tick(market(a), NOW)
            e.db.close(); return seen
        dual, alone = decisions(True), decisions(False)
        self.assertEqual([d for d in dual if d[0] == 'PAPER'], alone)
        self.assertEqual([d[1:] for d in dual if d[0] == 'SHADOW'], [d[1:] for d in alone])

    def test_shadow_kill_switch_stops_only_shadow_entries(self):
        self.cfg(kill_switch=True)
        self.e.control('start'); self.e.tick(market('ETH', yes_ask=.5), NOW)
        self.e.s['equity_peak']['shadow'] = 2000                                  # shadow ledger 50% below its peak
        self.e.tick(market(), NOW)
        self.assertIn('shadow', self.e.s['kill']); self.assertTrue(self.e.running)
        self.assertIn('BTC', self.e.s['positions']); self.assertNotIn('BTC', self.e.s['shadow_positions'])

    def test_shadow_alongside_live_small(self):
        from helpers import FakeExecutor
        x = FakeExecutor()
        e = Engine(self.path + 'L', executor=x); self.addCleanup(e.db.close)
        e.s['config'].update(dual_shadow=True, contracts=5); e.set_mode('live'); e.control('start')
        e.tick(market(), NOW)
        self.assertEqual((e.s['positions']['BTC']['size'], e.s['shadow_positions']['BTC']['size']), (1, 5))
        self.assertEqual(len([c for c in x.calls if c[0] == 'open']), 1)          # only the live ledger sends orders

    def test_watched_assets_and_pending_settings_cover_both_ledgers(self):
        self.e.control('start'); self.e.tick(market('XRP'), NOW)                  # not selected: no entries
        self.e.s['shadow_positions']['HYPE'] = dict(ticker='KXHYPE15M-T1', side='yes', entry=.85, size=1, entry_fee=.01, cost=.86, opened=NOW, mark=.83, mode='shadow')
        self.assertIn('HYPE', self.e.watched_assets())
        self.assertTrue(self.e.configure(dict(self.e.s['config'], dollars=30))['pending'])

    def test_migrates_shadow_positions_out_of_the_paper_ledger(self):
        old = dict(config=dict(DEFAULTS), cash=1000, trades=[], events=[], seen=['KXBTC15M-T1'], curve=[],
                   positions=dict(BTC=dict(ticker='KXBTC15M-T1', side='yes', entry=.85, size=1, entry_fee=.01, cost=.86, opened=1, mark=.8, mode='shadow'),
                                  ETH=dict(ticker='KXETH15M-T1', side='yes', entry=.85, size=1, entry_fee=.01, cost=.86, opened=1, mark=.8, mode='paper')))
        path = self.path + 'old'; db = sqlite3.connect(path)
        db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)'); db.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(old),)); db.commit(); db.close()
        e = Engine(path)
        self.assertEqual((sorted(e.s['positions']), sorted(e.s['shadow_positions'])), (['ETH'], ['BTC']))
        self.assertEqual(e.s['shadow_seen'], ['KXBTC15M-T1']); e.db.close()


class LatencyTimingTests(EngineCase):
    def test_due_orders_are_offered_once_per_retry(self):
        self.cfg(shadow_latency_ms=250); self.e.set_mode('shadow'); self.e.control('start'); self.e.tick(market(), NOW)
        self.assertEqual(self.e.sim_due(NOW + .1), [])                                  # not due yet
        self.assertEqual(self.e.sim_due(NOW + .3), [('BTC', 'KXBTC15M-T1')])
        self.assertEqual(self.e.sim_due(NOW + .4), [])                                  # already offered
        self.assertEqual(self.e.sim_due(NOW + 1.4), [('BTC', 'KXBTC15M-T1')])           # retry after a failed fetch

    def test_fresh_snapshot_at_due_time_fills_at_that_book(self):
        self.cfg(shadow_latency_ms=250); self.e.set_mode('shadow'); self.e.control('start'); self.e.tick(market(), NOW)
        self.e.tick(market(fetched=NOW + .26), NOW + .26)                                # fetched right when due
        self.assertEqual(self.e.s['shadow_positions']['BTC']['opened'], NOW + .26)

    def test_older_snapshot_arriving_late_is_ignored(self):
        self.e.tick(market(yes_ask=.5, fetched=NOW + 2), NOW + 2)
        self.e.tick(market(yes_ask=.6, fetched=NOW + 1), NOW + 2)
        self.assertEqual(self.e.markets['BTC']['yes_ask'], .5)


class DecisionAuditModeTests(EngineCase):
    def test_entry_decisions_are_logged_under_their_own_ledger(self):
        self.cfg(dual_shadow=True, audit_full=True)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        modes = sorted(ev['mode'] for ev in self.events() if ev['event'] == 'decision_entry')
        self.assertEqual(modes, ['paper', 'shadow'])
