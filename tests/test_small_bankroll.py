"""Small-bankroll controls (all off by default): per-trade worst-case loss, one market per session, and the
experimental cross-coin agreement filter (observable data only)."""
from helpers import CLOSE, NOW, EngineCase, market
from settings import DEFAULTS, validate

NO_FAVORED = dict(yes_ask=.17, yes_bid=.15, no_ask=.85, no_bid=.83)
DEEP = dict(yes_ask_size=100, no_ask_size=100)
PLAN = dict(size_mode='dollars', dollars=10, max_trade_risk=10, max_session_risk=10, max_session_entries=1, daily_loss=40, max_same_side=0)


class DefaultsTests(EngineCase):
    def test_off_by_default_and_valid(self):
        self.assertEqual((DEFAULTS['max_trade_risk'], DEFAULTS['max_session_entries'], DEFAULTS['agreement_min']), (0, 0, 0))
        validate(dict(DEFAULTS, assets=list(DEFAULTS['assets'])))
        for bad in (dict(max_trade_risk=-1), dict(max_session_entries=1.5), dict(max_session_entries=51), dict(agreement_min=1.1)):
            with self.assertRaises(ValueError, msg=bad):
                validate(dict(DEFAULTS, assets=list(DEFAULTS['assets']), **bad))

    def test_defaults_trade_several_coins_per_session(self):
        self.cfg(max_same_side=0); self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL'): self.e.tick(market(a), NOW)
        self.assertEqual(len(self.e.s['positions']), 3)


class TradeRiskTests(EngineCase):
    def test_skips_a_trade_that_could_lose_more_than_the_limit(self):
        self.cfg(contracts=20, max_trade_risk=10); self.e.control('start')
        self.e.tick(market('BTC', **DEEP), NOW)                     # 20 x 85c = $17+ at risk
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('Trade risk', self.e.snapshot()['blocked']['BTC'])
        self.cfg(contracts=11); self.e.tick(market('BTC', fetched=NOW + 1, **DEEP), NOW + 1)   # ~$9.4: allowed
        self.assertIn('BTC', self.e.s['positions'])
        self.assertLessEqual(self.e.s['positions']['BTC']['cost'], 10)


class SessionEntryTests(EngineCase):
    def test_one_market_per_session_across_coins_and_sides(self):
        self.cfg(max_same_side=0, max_session_entries=1); self.e.control('start')
        self.e.tick(market('BTC'), NOW); self.e.tick(market('ETH', **NO_FAVORED), NOW); self.e.tick(market('SOL'), NOW)
        self.assertEqual(list(self.e.s['positions']), ['BTC'])
        self.assertIn('Session entry cap', self.e.snapshot()['blocked']['ETH'])
        self.e.tick(market('ETH', ticker='KXETH15M-T2', close=CLOSE + 900, fetched=NOW + 800), NOW + 800)
        self.assertIn('ETH', self.e.s['positions'])                 # next session: a new allowance

    def test_the_hundred_dollar_plan(self):
        self.cfg(**PLAN); self.e.control('start')
        for a in ('BTC', 'ETH', 'SOL', 'DOGE'): self.e.tick(market(a, **DEEP), NOW)
        self.assertEqual(len(self.e.s['positions']), 1)
        p = next(iter(self.e.s['positions'].values()))
        self.assertLessEqual(p['cost'], 10)                          # worst case of the one open trade


class AgreementTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(max_same_side=0)

    def others(self, *fav, fetched=NOW):
        for a, f in zip(('ETH', 'SOL', 'DOGE'), fav, strict=False):
            self.e.tick(market(a, fetched=fetched, **(NO_FAVORED if f == 'no' else {})), fetched)

    def test_threshold(self):
        self.cfg(agreement_min=.6); self.others('yes', 'no', 'no')        # 1 of 3 agree
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('Agreement 33% < 60%', self.e.snapshot()['blocked']['BTC'])

    def test_enters_when_enough_agree(self):
        self.cfg(agreement_min=.6); self.others('yes', 'yes', 'no')       # 2 of 3 = 67%
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_only_quotes_known_before_the_decision_count(self):
        self.cfg(agreement_min=.6)
        self.others('yes', 'yes', 'yes', fetched=NOW + 5)               # fetched after the decision time: invisible
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('no other market data', self.e.snapshot()['blocked']['BTC'])
