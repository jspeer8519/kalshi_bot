"""Item 3: paper realism - whole contracts, walking order-book depth, configurable latency."""
from fills import ask_levels, simulate
from helpers import CLOSE, NOW, EngineCase, market


class FillModelTests(EngineCase):
    def test_ask_ladder_comes_from_other_side_bids(self):
        m = market(no_bids=[[.15, 4], [.12, 6]])
        self.assertEqual(ask_levels(m, 'yes'), [(.85, 4), (.88, 6)])
        self.assertEqual(ask_levels(market(), 'yes'), [(.85, 10)])            # no ladder: best level only

    def test_legacy_entry_uses_best_level_only(self):
        m = market(no_bids=[[.15, 4], [.12, 6]])
        self.assertEqual(simulate(m, 'yes', 'entry', 8, .88, depth=False), (4, .85, .85))

    def test_depth_entry_walks_levels_up_to_limit(self):
        m = market(no_bids=[[.15, 4], [.12, 6], [.05, 50]])
        n, avg, worst = simulate(m, 'yes', 'entry', 8, .88, depth=True)
        self.assertEqual((n, worst), (8, .88)); self.assertAlmostEqual(avg, (4*.85 + 4*.88) / 8)
        self.assertEqual(simulate(m, 'yes', 'entry', 8, .85, depth=True)[0], 4)   # never above the limit

    def test_whole_contracts_round_down(self):
        m = market(no_bids=[[.15, 2.5]])
        self.assertEqual(simulate(m, 'yes', 'entry', 5, .85, whole=False)[0], 2.5)
        self.assertEqual(simulate(m, 'yes', 'entry', 5, .85, whole=True)[0], 2)
        self.assertEqual(simulate(market(no_bids=[[.15, .6]]), 'yes', 'entry', 5, .85, whole=True), (0, None, None))
        self.assertEqual(simulate(market(yes_bids=[[.5, 1.7]]), 'yes', 'exit', 3, None, whole=True)[0], 1)


class PaperRealismEngineTests(EngineCase):
    def test_flags_off_keep_legacy_paper_fill(self):
        self.e.control('start'); self.e.tick(market(no_bids=[[.15, 10]]), NOW)
        p = self.e.s['positions']['BTC']; self.assertEqual((p['size'], p['entry']), (1, .85))
        self.assertAlmostEqual(self.e.s['cash'], 999.14)

    def test_whole_contracts_on_exit(self):
        self.cfg(paper_whole_contracts=True, stop_seconds=0, contracts=3)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.e.tick(market(yes_bid=.30, yes_bids=[[.30, 1.5], [.20, .4]], fetched=NOW+1), NOW+1)
        self.assertEqual(self.e.s['trades'][0]['size'], 1); self.assertEqual(self.e.s['positions']['BTC']['size'], 2)

    def test_latency_entry_fills_on_first_snapshot_after_due(self):
        self.cfg(paper_latency_ms=1500, paper_depth_fill=True)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertNotIn('BTC', self.e.s['positions']); self.assertEqual(len(self.e.s['sim_orders']), 1)
        self.assertIn('KXBTC15M-T1', self.e.s['seen'])                        # cannot be entered twice
        self.e.tick(market(no_bids=[[.16, 10]], fetched=NOW+1), NOW+1)         # before due: still in flight
        self.assertEqual(len(self.e.s['sim_orders']), 1)
        self.e.tick(market(no_bids=[[.16, 10]], fetched=NOW+2), NOW+2)         # ask improved to 84c by arrival
        p = self.e.s['positions']['BTC']; self.assertEqual((p['entry'], p['opened']), (.84, NOW+2))
        self.assertEqual(self.e.s['sim_orders'], [])

    def test_latency_entry_misses_when_ask_moves_above_limit(self):
        self.cfg(paper_latency_ms=1000)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.e.tick(market(yes_ask=.90, no_bid=.10, no_bids=[[.10, 10]], fetched=NOW+2), NOW+2)
        self.assertNotIn('BTC', self.e.s['positions']); self.assertAlmostEqual(self.e.s['cash'], 1000)
        self.assertTrue(any('no fill' in msg for msg in self.messages()))
        self.e.tick(market(fetched=NOW+3), NOW+3)                               # IOC missed: market stays used
        self.assertNotIn('BTC', self.e.s['positions'])

    def test_latency_order_expires_if_market_closes_first(self):
        self.cfg(paper_latency_ms=5000)
        self.e.control('start'); self.e.tick(market(close=NOW+3), NOW)
        self.e.tick(market(close=NOW+3, status='closed', fetched=NOW+6), NOW+6)
        self.assertEqual(self.e.s['sim_orders'], []); self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('sim_order_expired', [ev['event'] for ev in self.events()])

    def test_latency_exit(self):
        self.cfg(stop_seconds=0)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.cfg(paper_latency_ms=1000)
        self.e.tick(market(yes_bid=.30, fetched=NOW+1), NOW+1)                 # stop decided, order in flight
        self.e.tick(market(yes_bid=.30, fetched=NOW+1.5), NOW+1.5)             # no duplicate exit order
        self.assertEqual(len(self.e.s['sim_orders']), 1)
        self.e.tick(market(yes_bid=.25, yes_bids=[[.25, 10]], fetched=NOW+2.5), NOW+2.5)
        self.assertNotIn('BTC', self.e.s['positions']); self.assertEqual(self.e.s['trades'][0]['exit'], .25)

    def test_restart_expires_in_flight_orders(self):
        self.cfg(paper_latency_ms=1000)
        self.e.control('start'); self.e.tick(market(), NOW)
        other = self.make(); self.assertEqual(other.s['sim_orders'], []); other.db.close()

    def test_in_flight_entry_counts_toward_daily_loss_risk(self):
        self.cfg(paper_latency_ms=1000, max_same_side=0, size_mode='dollars', dollars=30, daily_loss=50)
        self.e.control('start'); self.e.tick(market('BTC', yes_ask_size=100), NOW)
        self.e.tick(market('ETH', yes_ask_size=100), NOW)
        self.assertIn('daily loss limit', self.e.snapshot()['blocked']['ETH'])
        self.assertEqual(CLOSE, self.e.s['sim_orders'][0]['close'])
