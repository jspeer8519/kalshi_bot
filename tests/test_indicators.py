import unittest
from indicators import ema, rsi, atr, summarize


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_step(self):
        out = ema([1, 2, 3, 4], 3)
        self.assertEqual(out[:2], [None, None]); self.assertAlmostEqual(out[2], 2); self.assertAlmostEqual(out[3], 3)

    def test_rsi_extremes_and_bounds(self):
        self.assertEqual(rsi(list(range(20)), 14)[-1], 100.0)
        self.assertEqual(rsi(list(range(20, 0, -1)), 14)[-1], 0.0)
        self.assertEqual(rsi([5] * 20, 14)[-1], 50.0)
        mixed = rsi([10, 11, 10.5, 11.5, 11, 12, 11.2, 12.4, 12, 12.8, 12.1, 13, 12.6, 13.4, 13, 13.9, 13.1], 14)
        self.assertTrue(all(v is None or 0 <= v <= 100 for v in mixed)); self.assertIsNone(mixed[13])

    def test_atr_true_range_uses_gap(self):
        candles = [dict(h=10, l=9, c=9.5)] + [dict(h=11, l=10, c=10.5)] * 14   # 1st TR includes gap from 9.5
        self.assertAlmostEqual(atr(candles, 14)[-1], (1.5 + 13 * 1) / 14)

    def test_summary_alignment(self):
        candles = [dict(t=i, o=100 + i, h=101 + i, l=99 + i, c=100 + i) for i in range(100)]
        s = summarize(candles, 60)
        self.assertEqual(len(s['ema9']), 60); self.assertEqual(len(s['rsi14']), 60); self.assertIsNotNone(s['ema21'][0])
        self.assertGreater(s['atr14'], 0)


if __name__ == '__main__':
    unittest.main()
