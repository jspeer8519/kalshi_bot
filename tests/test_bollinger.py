"""Bollinger Bands on 1-minute closes: the math, the data rules, and the optional entry filter (default off)."""
import math
import statistics
import unittest

import indicators as ind
from helpers import NOW, EngineCase, market
from settings import DEFAULTS, validate

NO_FAVORED = dict(yes_ask=.17, yes_bid=.15, no_ask=.85, no_bid=.83)


def ticks(end=NOW, minutes=21, base=100.0, last_30s=None):
    """1-second ticks with a small wiggle; optionally the final 30 seconds at ``last_30s``."""
    out = [(t, base + math.sin(t / 7) * .2) for t in range(end - minutes * 60, end + 1)]
    if last_30s is not None:
        out = [(t, last_30s if t > end - 30 else p) for t, p in out]
    return out


class MathTests(unittest.TestCase):
    def test_bands_are_sma_plus_minus_k_population_sd(self):
        closes = [float(x) for x in (3, 5, 4, 8, 6, 7, 9, 5, 6, 10)]
        mid, up, lo = ind.bollinger(closes, 5, 2.0)
        self.assertEqual(mid[:4], [None] * 4)
        w = closes[-5:]
        self.assertAlmostEqual(mid[-1], statistics.fmean(w))
        self.assertAlmostEqual(up[-1], statistics.fmean(w) + 2 * statistics.pstdev(w))
        self.assertAlmostEqual(lo[-1], statistics.fmean(w) - 2 * statistics.pstdev(w))

    def test_percent_b(self):
        self.assertEqual(ind.percent_b(10, 12, 8), .5)
        self.assertEqual(ind.percent_b(8, 12, 8), 0)
        self.assertLess(ind.percent_b(7, 12, 8), 0)
        self.assertEqual(ind.percent_b(5, 5, 5), .5)          # flat bands: neutral, no division by zero

    def test_summarize_adds_bands_for_the_chart(self):
        candles = [dict(t=60 * i, o=100, h=101, l=99, c=100 + (i % 3)) for i in range(40)]
        s = ind.summarize(candles, 30)
        self.assertEqual(len(s['bb_up']), 30)
        self.assertIsNotNone(s['bb_pctb'])


class DataRuleTests(unittest.TestCase):
    def test_minute_closes_use_last_tick_and_the_forming_minute(self):
        tk = [(NOW - 120, 1.0), (NOW - 61, 2.0), (NOW - 59, 3.0), (NOW - 1, 4.0)]
        now = (NOW // 60) * 60 + 30
        c = ind.minute_closes([(t - NOW + now, p) for t, p in tk], now)
        self.assertEqual(c[-1], 4.0)

    def test_future_ticks_are_ignored(self):
        tk = ticks() + [(NOW + 1, 50.0), (NOW + 2, 50.0)]
        self.assertGreater(ind.bollinger_now(tk, NOW)['price'], 99)

    def test_a_feed_gap_is_never_bridged(self):
        tk = [(t, p) for t, p in ticks() if not NOW - 600 <= t < NOW - 480]    # two missing minutes
        self.assertLess(len(ind.minute_closes(tk, NOW)), 20)
        self.assertIsNone(ind.bollinger_now(tk, NOW))

    def test_stale_prices_give_nothing(self):
        self.assertEqual(ind.minute_closes(ticks(end=NOW - 10), NOW), [])

    def test_needs_n_minutes(self):
        self.assertIsNone(ind.bollinger_now(ticks(minutes=10), NOW, 20))
        self.assertIsNotNone(ind.bollinger_now(ticks(minutes=10), NOW, 5))


class SettingsTests(unittest.TestCase):
    def test_defaults_off_and_valid(self):
        self.assertFalse(DEFAULTS['bb_filter'])
        validate(dict(DEFAULTS, assets=list(DEFAULTS['assets'])))

    def test_bounds(self):
        for bad in (dict(bb_period=4), dict(bb_period=21), dict(bb_period=10.5), dict(bb_k=.5), dict(bb_k=5), dict(bb_pctb=-.1), dict(bb_pctb=.6)):
            with self.assertRaises(ValueError, msg=bad):
                validate(dict(DEFAULTS, assets=list(DEFAULTS['assets']), **bad))


class FilterTests(EngineCase):
    def test_off_by_default_enters_even_on_a_crash(self):
        self.spot['BTC'] = ticks(last_30s=95.0)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_skips_yes_while_price_breaks_the_lower_band(self):
        self.cfg(bb_filter=True); self.spot['BTC'] = ticks(last_30s=95.0)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('lower band break against YES', self.e.snapshot()['blocked']['BTC'])

    def test_enters_yes_on_a_breakout_up_and_skips_no(self):
        self.cfg(bb_filter=True, max_same_side=0); self.spot['BTC'] = ticks(last_30s=105.0); self.spot['ETH'] = ticks(last_30s=105.0)
        self.e.control('start'); self.e.tick(market('BTC'), NOW); self.e.tick(market('ETH', **NO_FAVORED), NOW)
        self.assertIn('BTC', self.e.s['positions'])
        self.assertNotIn('ETH', self.e.s['positions'])
        self.assertIn('upper band break against NO', self.e.snapshot()['blocked']['ETH'])

    def test_enters_inside_the_bands(self):
        self.cfg(bb_filter=True); self.spot['BTC'] = ticks()
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_pctb_threshold_widens_the_filter(self):
        self.cfg(bb_filter=True, bb_pctb=.5); self.spot['BTC'] = ticks(last_30s=99.9)   # just under the middle band
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])

    def test_unavailable_history_skips(self):
        self.cfg(bb_filter=True); self.spot['BTC'] = ticks(minutes=5)
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('not enough price history', self.e.snapshot()['blocked']['BTC'])

    def test_crash_after_the_decision_time_is_not_used(self):
        self.cfg(bb_filter=True); self.spot['BTC'] = ticks() + [(NOW + s, 90.0) for s in range(1, 30)]
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertIn('BTC', self.e.s['positions'])


if __name__ == '__main__':
    unittest.main()
