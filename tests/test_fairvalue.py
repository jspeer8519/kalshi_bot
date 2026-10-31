"""Item 6: fair-value model and the entry/exit rules that use it."""
import math
import unittest

import fairvalue
from fairvalue import prob_yes, variance_per_second
from helpers import CLOSE, NOW, EngineCase, market

K = 100.0                 # market target (60-second index average before open)
D = .0005                 # each 1-second log return is +-D -> variance D^2 per second
WIGGLE = (0, 1, 0, -1)    # NOW % 4 == 0, so the latest tick sits exactly on the level


def ticks(level_now, start=NOW-1000, end=NOW, switch=NOW-300, level_before=K, noise=D):
    """1-second ticks at ``level_before`` (which makes the pre-open basis ~0), then ``level_now``."""
    return [(t, (level_before if t < switch else level_now) * math.exp(noise * WIGGLE[t % 4])) for t in range(start, end + 1)]


class ModelTests(unittest.TestCase):
    def test_variance_estimate(self):
        self.assertAlmostEqual(variance_per_second(ticks(K), NOW, 600), D * D, places=12)
        self.assertIsNone(variance_per_second(ticks(K)[-30:], NOW, 600))          # not enough history

    def test_at_the_money_is_near_half_and_monotonic(self):
        mid = prob_yes(ticks(K), K, CLOSE, NOW)['prob']
        self.assertAlmostEqual(mid, .5, delta=.01)
        up, down = prob_yes(ticks(K*1.003), K, CLOSE, NOW)['prob'], prob_yes(ticks(K*.997), K, CLOSE, NOW)['prob']
        self.assertGreater(up, mid); self.assertLess(down, mid); self.assertGreater(prob_yes(ticks(K*1.03), K, CLOSE, NOW)['prob'], .99)   # the 3% step itself adds variance

    def test_variance_uses_time_to_window_plus_a_third_of_the_window(self):
        tk = ticks(K*1.002)
        r = prob_yes(tk, K, CLOSE, NOW)
        var = variance_per_second(tk, NOW, 900) * ((CLOSE - 60 - NOW) + 20)
        spot = r['index_proxy']
        self.assertAlmostEqual(r['prob'], 0.5 * (1 + math.erf((math.log(spot / K) - var / 2) / math.sqrt(var) / math.sqrt(2))), places=6)

    def test_basis_correction_from_pre_open_minute(self):
        r = prob_yes(ticks(K + 5, level_before=K + 5, switch=NOW-2000), K, CLOSE, NOW)   # Coinbase sat $5 above the index all along
        self.assertAlmostEqual(r['basis'], 5, delta=.01); self.assertTrue(r['adjusted'])
        self.assertAlmostEqual(r['prob'], .5, delta=.01)
        late = prob_yes(ticks(K + 5, start=NOW-500, switch=NOW-2000), K, CLOSE, NOW)    # app missed that minute
        self.assertFalse(late['adjusted']); self.assertGreater(late['prob'], .99)

    def test_inside_window_observed_average_counts(self):
        now = CLOSE - 30                                                          # 30 of 60 seconds observed
        flat = [(t, K - 3) for t in range(NOW-1000, CLOSE - 60)] + [(t, K - 3) for t in range(CLOSE - 60, now - 2)] + [(now-1, K+1), (now, K+1)]
        r = prob_yes(flat, K - 3, CLOSE, now)
        self.assertEqual(r['prob'], 1.0)                                          # zero vol, already at the target
        # Spot is above the target, but the window so far averaged $3 below, so the rest must average K+3:
        # almost surely NO, although comparing spot with the target alone would say YES.
        hist = [(t, K) for t in range(NOW-1000, CLOSE - 60)] + [(t, K - 3) for t in range(CLOSE - 60, now)] + [(now, K + 1)]
        self.assertLess(prob_yes(hist, K, CLOSE, now, lookback=1200)['prob'], .001)

    def test_tie_at_close_resolves_yes(self):
        hist = [(t, K) for t in range(NOW-1000, CLOSE + 1)]
        self.assertEqual(prob_yes(hist, K, CLOSE, CLOSE)['prob'], 1.0)

    def test_no_look_ahead(self):
        past = ticks(K * 1.001)
        future = past + [(t, K * 2) for t in range(NOW + 1, NOW + 60)]
        self.assertEqual(prob_yes(past, K, CLOSE, NOW), prob_yes(future, K, CLOSE, NOW))

    def test_fails_closed_without_inputs(self):
        self.assertIsNone(prob_yes(ticks(K), None, CLOSE, NOW))                    # no target
        self.assertIsNone(prob_yes(ticks(K, end=NOW - 10), K, CLOSE, NOW))          # stale spot
        self.assertIsNone(prob_yes([], K, CLOSE, NOW))
        self.assertGreaterEqual(fairvalue.MAX_SPOT_AGE, 1)


class FairValueEngineTests(EngineCase):
    def setUp(self):
        super().setUp(); self.cfg(fair_value=True, edge_margin=.02)

    def test_enters_only_with_edge_after_fees(self):
        self.spot['BTC'] = ticks(K * 1.03)                                        # fair ~0.999 vs 85c ask
        self.e.control('start'); self.e.tick(market(strike=K), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_skips_without_edge(self):
        self.spot['BTC'] = ticks(K)                                               # fair ~0.50 vs 85c ask
        self.e.control('start'); self.e.tick(market(strike=K), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('No edge', self.e.snapshot()['blocked']['BTC'])

    def test_edge_margin_is_respected(self):
        self.spot['BTC'] = ticks(K * 1.03)
        self.cfg(edge_margin=.2)                                                  # .999 - .85 - .01 < .20
        self.e.control('start'); self.e.tick(market(strike=K), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])

    def test_skips_when_model_unavailable(self):
        self.e.control('start'); self.e.tick(market(strike=K), NOW)                # no price ticks
        self.assertIn('Fair value unavailable', self.e.snapshot()['blocked']['BTC'])

    def test_off_by_default_ignores_model(self):
        self.cfg(fair_value=False)
        self.spot['BTC'] = ticks(K)
        self.e.control('start'); self.e.tick(market(strike=K), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def enter(self):
        self.spot['BTC'] = ticks(K * 1.03)
        self.e.control('start'); self.e.tick(market(strike=K), NOW)

    def test_holds_while_bid_below_fair_even_past_take_profit(self):
        self.enter()
        self.e.tick(market(strike=K, yes_bid=.96, yes_bids=[[.96, 10]], fetched=NOW), NOW)    # take-profit level, fair ~.999
        self.assertIn('BTC', self.e.s['positions'])

    def test_exits_when_bid_above_fair_and_ignores_stop(self):
        self.enter()
        self.spot['BTC'] = ticks(K * .97)                                         # fair YES ~0.001
        self.e.tick(market(strike=K, yes_bid=.30, yes_bids=[[.30, 10]], fetched=NOW), NOW)
        t = self.e.s['trades'][0]
        self.assertEqual((t['reason'], t['exit']), ('fair value exit', .30))

    def test_never_sells_below_fair_plus_margin(self):
        self.cfg(contracts=10); self.enter()
        self.spot['BTC'] = ticks(K * .9995)                                       # fair a bit under 50%
        fair = prob_yes(self.spot['BTC'], K, CLOSE, NOW)['prob']
        self.cfg(fv_exit_margin=.05)
        self.e.tick(market(strike=K, yes_bid=fair + .03, yes_bids=[[fair + .03, 10]], fetched=NOW), NOW)
        self.assertIn('BTC', self.e.s['positions'])                                # above fair, but not by the margin
        self.e.tick(market(strike=K, yes_bid=fair + .06, yes_bids=[[fair + .06, 4], [fair + .01, 6]], fetched=NOW), NOW)
        self.assertEqual(self.e.s['trades'][0]['size'], 4)                         # deeper levels are below the floor

    def test_stop_still_applies_when_model_unavailable(self):
        self.enter()
        self.spot['BTC'] = []
        self.cfg(stop_seconds=0)
        self.e.tick(market(strike=K, yes_bid=.30, fetched=NOW), NOW)
        self.assertEqual(self.e.s['trades'][0]['reason'], 'stop loss')
