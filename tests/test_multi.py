"""Several markets traded side by side; each runs the same rules independently."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine import Engine, DEFAULTS, validate


class MultiMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = str(Path(self.temp.name) / 'p.db')
        self.e = Engine(self.path); self.now = 1800000000
        self.e.s['config']['max_same_side'] = 0      # these tests enter several markets on the same side

    def tearDown(self):
        self.e.db.close(); self.temp.cleanup()

    def m(self, asset, **kw):
        base = dict(asset=asset, ticker=f'KX{asset}15M-T1', status='active', result='', close=self.now+120, fetched=self.now,
                    yes_ask=.85, no_ask=.17, yes_bid=.83, no_bid=.15, yes_ask_size=10, no_ask_size=10, yes_bid_size=10, no_bid_size=10)
        return dict(base, **kw)

    def test_defaults_are_four_markets(self):
        self.assertEqual(DEFAULTS['assets'], ['BTC', 'ETH', 'SOL', 'DOGE'])

    def test_each_market_gets_its_own_position(self):
        self.e.control('start')
        for a in ['BTC', 'ETH', 'SOL', 'DOGE']: self.e.tick(self.m(a), self.now)
        self.assertEqual(sorted(self.e.s['positions']), ['BTC', 'DOGE', 'ETH', 'SOL'])
        self.assertTrue(all(p['size'] == 1 for p in self.e.s['positions'].values()))
        self.assertAlmostEqual(self.e.s['cash'], 1000 - 4 * .86)

    def test_one_entry_per_market_session(self):
        self.e.control('start'); self.e.tick(self.m('ETH'), self.now)
        self.e.tick(self.m('ETH', yes_bid=.96, fetched=self.now+1), self.now+1)          # take profit
        self.assertNotIn('ETH', self.e.s['positions'])
        self.e.tick(self.m('ETH', fetched=self.now+2), self.now+2)                        # same session: no re-entry
        self.assertNotIn('ETH', self.e.s['positions'])
        self.e.tick(self.m('ETH', ticker='KXETH15M-T2', fetched=self.now+3), self.now+3)  # next session: allowed
        self.assertIn('ETH', self.e.s['positions'])

    def test_stop_on_one_market_does_not_touch_another(self):
        self.e.control('start'); self.e.tick(self.m('BTC'), self.now); self.e.tick(self.m('SOL'), self.now)
        self.e.tick(self.m('SOL', yes_bid=.30), self.now); self.e.tick(self.m('SOL', yes_bid=.30, fetched=self.now+1), self.now+1)
        self.assertNotIn('SOL', self.e.s['positions']); self.assertIn('BTC', self.e.s['positions'])
        self.assertEqual(self.e.s['trades'][0]['asset'], 'SOL')

    def test_unselected_market_not_traded(self):
        self.e.control('start'); self.e.tick(self.m('XRP'), self.now)
        self.assertNotIn('XRP', self.e.s['positions'])

    def test_shared_daily_loss_limit_pauses_all(self):
        self.e.control('start')
        for a in ['BTC', 'ETH']: self.e.tick(self.m(a), self.now)
        self.e.s['config']['daily_loss'] = .5
        for a in ['BTC', 'ETH']:
            self.e.tick(self.m(a, yes_bid=.30), self.now); self.e.tick(self.m(a, yes_bid=.30, fetched=self.now+1), self.now+1)
        self.assertFalse(self.e.running)
        self.e.tick(self.m('SOL', fetched=self.now+2), self.now+2); self.assertNotIn('SOL', self.e.s['positions'])

    def test_feed_error_is_per_market(self):
        self.e.tick(self.m('BTC'), self.now); self.e.feed_error('ETH', 'down')
        snap = self.e.snapshot(); self.assertEqual(snap['errors'], {'ETH': 'down'}); self.assertIn('BTC', snap['markets'])

    def test_settings_require_markets(self):
        for bad in [[], ['BTC', 'BTC'], ['NOPE'], 'BTC']:
            with self.assertRaises(ValueError): validate(dict(DEFAULTS, assets=bad))
        self.assertEqual(validate(dict(DEFAULTS, assets=['DOGE', 'BTC']))['assets'], ['BTC', 'DOGE'])

    def test_migrates_single_market_state(self):
        old = dict(config=dict(initial_balance=1000, asset='BTC', contracts=1, entry_min=.7, entry_max=.9, stop=.4, take_profit=.1,
                               window=6, stop_seconds=1, daily_loss=200, daily_profit=400),
                   cash=999.1, position=dict(ticker='KXBTC15M-OLD', side='yes', entry=.88, size=1, entry_fee=.01, cost=.89, opened=1, mark=.9),
                   trades=[], events=[], seen=[], curve=[dict(t=1, value=1000)])
        path = str(Path(self.temp.name) / 'old.db'); db = sqlite3.connect(path)
        db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)'); db.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(old),)); db.commit(); db.close()
        e = Engine(path)
        self.assertEqual(e.s['config']['assets'], ['BTC', 'ETH', 'SOL', 'DOGE']); self.assertEqual(e.s['config']['entry_min'], .7)
        self.assertEqual(e.s['positions']['BTC']['ticker'], 'KXBTC15M-OLD'); self.assertNotIn('position', e.s)
        validate(json.loads(json.dumps(e.s['config']))); e.db.close()

    def test_entry_blocked_when_worst_case_breaks_daily_loss(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, daily_loss=100); self.e.control('start')
        for a in ['BTC', 'ETH', 'SOL', 'DOGE']: self.e.tick(self.m(a, yes_ask_size=100), self.now)
        self.assertEqual(sorted(self.e.s['positions']), ['BTC', 'ETH', 'SOL'])   # 4th $30 would risk >$100
        self.assertNotIn('KXDOGE15M-T1', self.e.s['seen'])                        # may still enter later this session
        self.assertTrue(any('daily loss limit' in ev['message'] for ev in self.e.s['events']))

    def test_guard_logs_once_per_market_and_shows_reason(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, daily_loss=50); self.e.control('start')
        self.e.tick(self.m('BTC', yes_ask_size=100), self.now)
        for i, ask in enumerate([.85, .84, .86, .85]):
            self.e.tick(self.m('ETH', yes_ask=ask, yes_ask_size=100, fetched=self.now+i), self.now+i)
        msgs = [ev for ev in self.e.s['events'] if 'ETH entry skipped' in ev['message']]
        self.assertEqual(len(msgs), 1)
        self.assertIn('daily loss limit', self.e.snapshot()['blocked']['ETH'])
        self.e.tick(self.m('ETH', ticker='KXETH15M-T2', yes_ask=.5, fetched=self.now+9), self.now+9)
        self.assertNotIn('ETH', self.e.snapshot()['blocked'])

    def test_stop_walks_book_depth(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, stop=.4, stop_seconds=0); self.e.control('start')
        self.e.tick(self.m('BTC', yes_ask_size=100), self.now); n = self.e.s['positions']['BTC']['size']
        book = [[.35, 10], [.30, 10], [.20, 100]]
        self.e.tick(self.m('BTC', yes_bid=.35, yes_bid_size=10, yes_bids=book, fetched=self.now+1), self.now+1)
        self.assertNotIn('BTC', self.e.s['positions']); t = self.e.s['trades'][0]
        self.assertEqual(t['size'], n); self.assertAlmostEqual(t['exit'], round((3.5 + 3.0 + .2 * (n - 20)) / n, 6))

    def test_stop_partial_when_book_too_thin(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, stop=.4, stop_seconds=0); self.e.control('start')
        self.e.tick(self.m('BTC', yes_ask_size=100), self.now); n = self.e.s['positions']['BTC']['size']
        self.e.tick(self.m('BTC', yes_bid=.35, yes_bid_size=10, yes_bids=[[.35, 10]], fetched=self.now+1), self.now+1)
        self.assertEqual(self.e.s['positions']['BTC']['size'], n - 10); self.assertEqual(self.e.s['trades'][0]['size'], 10)

    def test_take_profit_never_sells_below_target(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, take_profit=.1); self.e.control('start')
        self.e.tick(self.m('BTC', yes_ask_size=100), self.now); n = self.e.s['positions']['BTC']['size']
        self.e.tick(self.m('BTC', yes_bid=.95, yes_bid_size=5, yes_bids=[[.95, 5], [.90, 50]], fetched=self.now+1), self.now+1)
        self.assertEqual(self.e.s['trades'][0]['size'], 5); self.assertEqual(self.e.s['positions']['BTC']['size'], n - 5)

    def test_settlement_stall_alert(self):
        self.e.control('start'); self.e.tick(self.m('BTC'), self.now)
        self.e.tick(self.m('BTC', status='closed', fetched=self.now+1000), self.now+1000)
        self.assertTrue(any('has not settled' in ev['message'] for ev in self.e.s['events']))
        self.assertIn('BTC', self.e.s['positions'])

    def test_fixed_take_profit_price(self):
        self.e.s['config'].update(size_mode='dollars', dollars=30, take_profit=1, tp_price=.99, stop=0); self.e.control('start')
        self.e.tick(self.m('BTC', yes_ask_size=100), self.now); n = self.e.s['positions']['BTC']['size']
        self.e.tick(self.m('BTC', yes_bid=.98, yes_bid_size=100, fetched=self.now+1), self.now+1)
        self.assertIn('BTC', self.e.s['positions'])                                   # 98c: hold
        self.e.tick(self.m('BTC', yes_bid=.99, yes_bid_size=100, yes_bids=[[.99, 100]], fetched=self.now+2), self.now+2)
        t = self.e.s['trades'][0]; self.assertEqual((t['reason'], t['exit'], t['size']), ('take profit', .99, n))

    def test_tp_price_validation(self):
        validate(dict(DEFAULTS, tp_price=.99)); validate(dict(DEFAULTS, tp_price=0))
        for bad in (.5, 1, 1.2):
            with self.assertRaises(ValueError): validate(dict(DEFAULTS, tp_price=bad))


if __name__ == '__main__':
    unittest.main()
