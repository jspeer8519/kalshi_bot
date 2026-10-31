"""Items 4-5: same-direction cap per session, and the direction-agreement filter."""
from helpers import CLOSE, NOW, EngineCase, market

NO_FAVORED = dict(yes_ask=.17, yes_bid=.15, no_ask=.85, no_bid=.83)


class SameSideCapTests(EngineCase):
    def test_default_cap_allows_one_yes_per_session(self):
        self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL'): self.e.tick(market(a), NOW)
        self.assertEqual(list(self.e.s['positions']), ['BTC'])
        self.assertIn('Same-side cap', self.e.snapshot()['blocked']['ETH'])
        self.assertEqual(sum('YES entry already this session' in m for m in self.messages()), 2)

    def test_cap_is_per_side(self):
        self.e.control('start'); self.e.tick(market('BTC'), NOW); self.e.tick(market('ETH', **NO_FAVORED), NOW)
        self.assertEqual(self.e.s['positions']['ETH']['side'], 'no')

    def test_cap_counts_closed_positions_and_resets_next_session(self):
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.e.tick(market('BTC', yes_bid=.96, fetched=NOW+1), NOW+1)          # take profit: BTC flat again
        self.e.tick(market('ETH', fetched=NOW+2), NOW+2)
        self.assertNotIn('ETH', self.e.s['positions'])                          # still one YES this session
        self.e.tick(market('ETH', ticker='KXETH15M-T2', close=CLOSE+900, fetched=NOW+800), NOW+800)
        self.assertIn('ETH', self.e.s['positions'])

    def test_cap_of_two_and_zero_disables(self):
        self.cfg(max_same_side=2); self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL'): self.e.tick(market(a), NOW)
        self.assertEqual(sorted(self.e.s['positions']), ['BTC', 'ETH'])
        self.cfg(max_same_side=0); self.e.tick(market('SOL', fetched=NOW+1), NOW+1)
        self.assertIn('SOL', self.e.s['positions'])

    def test_cap_is_per_mode(self):
        self.e.control('start'); self.e.tick(market('BTC'), NOW); self.e.control('pause')
        self.e.set_mode('shadow'); self.e.control('start'); self.e.tick(market('ETH'), NOW)
        self.assertEqual(self.e.s['shadow_positions']['ETH']['mode'], 'shadow')


class DirectionFilterTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(max_same_side=0)

    def test_off_by_default(self):
        self.e.tick(market('ETH', **NO_FAVORED), NOW)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_skips_when_other_markets_disagree(self):
        self.cfg(direction_filter=True)
        self.e.tick(market('ETH', **NO_FAVORED), NOW); self.e.tick(market('SOL'), NOW)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('ETH disagree', self.e.snapshot()['blocked']['BTC'])

    def test_enters_when_others_agree(self):
        self.cfg(direction_filter=True)
        self.e.tick(market('ETH', yes_ask=.70, yes_bid=.68, fetched=NOW-1), NOW-1)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_uses_only_data_available_at_that_moment(self):
        self.cfg(direction_filter=True)
        self.e.tick(market('ETH', **NO_FAVORED, fetched=NOW+5), NOW+5)        # fetched after the decision time
        self.e.tick(market('SOL', **NO_FAVORED, fetched=NOW-30), NOW-30)      # too old
        self.e.tick(market('DOGE', **NO_FAVORED, close=CLOSE+900), NOW)        # a different session
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('no other market data', self.e.snapshot()['blocked']['BTC'])
        self.e.tick(market('SOL', fetched=NOW), NOW)                           # one fresh agreeing market
        self.e.tick(market('BTC', fetched=NOW+.5), NOW+.5)
        self.assertIn('BTC', self.e.s['positions'])

    def test_fifty_fifty_market_has_no_vote(self):
        from strategy import favorite
        self.assertIsNone(favorite(dict(yes_bid=.49, yes_ask=.51)))
        self.assertEqual(favorite(dict(yes_bid=.30, yes_ask=.33)), 'no')
