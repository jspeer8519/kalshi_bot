"""Kalshi contract price history for the dashboard charts."""
import unittest

from quotes import QuoteHistory, parse_candles


def snap(**kw):
    return dict(dict(asset='BTC', ticker='KXBTC15M-T1', status='active', close=1900, fetched=1000.0, yes_bid=.62, yes_ask=.63), **kw)


class QuoteHistoryTests(unittest.TestCase):
    def test_records_points_and_returns_newer_ones(self):
        h = QuoteHistory()
        h.add(snap()); h.add(snap(fetched=1002.0, yes_bid=.64, yes_ask=.66)); h.add(snap(fetched=1001.0))   # out of order: ignored
        self.assertEqual(h.since(0)['BTC'], dict(ticker='KXBTC15M-T1', close=1900, points=[[1000.0, .62, .63], [1002.0, .64, .66]]))
        self.assertEqual(h.since(1000.0)['BTC']['points'], [[1002.0, .64, .66]])

    def test_new_market_starts_fresh_and_inactive_is_ignored(self):
        h = QuoteHistory()
        h.add(snap()); h.add(snap(ticker='KXBTC15M-T2', close=2800, fetched=1905.0))
        h.add(snap(ticker='KXBTC15M-T2', status='closed', fetched=1906.0))
        self.assertEqual(h.since(0)['BTC'], dict(ticker='KXBTC15M-T2', close=2800, points=[[1905.0, .62, .63]]))

    def test_assets_are_kept_apart(self):
        h = QuoteHistory()
        h.add(snap()); h.add(snap(asset='ETH', ticker='KXETH15M-T1', yes_bid=.3, yes_ask=.31))
        out = h.since(0)
        self.assertEqual((out['BTC']['points'][0][1], out['ETH']['points'][0][1]), (.62, .3))

    def test_parse_candles(self):
        body = dict(candlesticks=[
            dict(end_period_ts=1060, yes_bid=dict(close_dollars='0.6200'), yes_ask=dict(close_dollars='0.6300')),
            dict(end_period_ts=1000, yes_bid=dict(close_dollars='0.0000'), yes_ask=dict(close_dollars='0.5100')),   # empty bid side
            dict(end_period_ts=1120, yes_bid=dict(close_dollars='0'), yes_ask=dict(close_dollars='1.0000')),        # empty book: skipped
            dict(yes_bid=dict(close_dollars='0.5')),                                                                # no timestamp
        ])
        self.assertEqual(parse_candles(body), [[1000.0, None, .51], [1060.0, .62, .63]])
        self.assertEqual(parse_candles({}), [])
