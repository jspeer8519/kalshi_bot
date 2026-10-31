import unittest
from pricefeed import TickFeed


class TickFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.2
        self.prices = {'BTC-USD': 100.0, 'ETH-USD': 10.0}
        def fetch(product):
            if product not in self.prices: raise OSError('down')
            return self.prices[product]
        self.feed = TickFeed({'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'SOL': 'SOL-USD'}, lambda: ['BTC', 'ETH', 'SOL', 'HYPE'], fetch=fetch, clock=lambda: self.now)

    def test_one_sample_per_second_and_since(self):
        self.feed.poll_once(); self.prices['BTC-USD'] = 101.0; self.feed.poll_once()   # same second: replaced
        self.now += 1; self.prices['BTC-USD'] = 102.0; self.feed.poll_once()
        out = self.feed.since(0)
        self.assertEqual(out['ticks']['BTC'], [[1800000000, 101.0], [1800000001, 102.0]])
        self.assertEqual(self.feed.since(1800000000)['ticks']['BTC'], [[1800000001, 102.0]])
        self.assertIn('SOL', out['errors']); self.assertNotIn('HYPE', out['ticks'])

    def test_error_clears_on_recovery(self):
        self.feed.poll_once(); self.prices['SOL-USD'] = 5.0; self.now += 1; self.feed.poll_once()
        self.assertNotIn('SOL', self.feed.since(0)['errors'])


if __name__ == '__main__':
    unittest.main()
