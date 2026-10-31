"""Kalshi contract price history for the dashboard charts (display only; never used for decisions).

Live points come from the order-book snapshots the feed already takes (about every 2 seconds).
Earlier parts of the current market come from Kalshi's public 1-minute candlesticks
(GET /series/{series}/markets/{ticker}/candlesticks?period_interval=1), closing YES bid/ask.
NO prices are the complement: NO ask = 1 - YES bid, NO bid = 1 - YES ask."""
import threading
import time
from collections import deque

KEEP = 600                 # live points per market: 15 minutes at 2-second refreshes, plus margin


def _price(v):
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 1 else None      # 0 / $1 mean an empty side of the book


def parse_candles(body):
    """[[end of minute, yes_bid, yes_ask]] from a candlesticks response, oldest first."""
    out = []
    for c in body.get('candlesticks') or []:
        try:
            t = float(c['end_period_ts'])
        except (KeyError, TypeError, ValueError):
            continue
        bid = _price((c.get('yes_bid') or {}).get('close_dollars'))
        ask = _price((c.get('yes_ask') or {}).get('close_dollars'))
        if bid is not None or ask is not None:
            out.append([t, bid, ask])
    return sorted(out)


class QuoteHistory:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.markets = {}                # asset -> dict(ticker, close, points deque[[t, yes_bid, yes_ask]])

    def add(self, snap):
        """Record one feed snapshot. A new ticker starts a fresh history for that asset."""
        asset, ticker = snap.get('asset'), snap.get('ticker')
        if not asset or not ticker or snap.get('status') != 'active':
            return
        point = [round(float(snap['fetched']), 3), snap.get('yes_bid'), snap.get('yes_ask')]
        with self.lock:
            m = self.markets.get(asset)
            if not m or m['ticker'] != ticker:
                m = self.markets[asset] = dict(ticker=ticker, close=snap.get('close'), points=deque(maxlen=KEEP))
            if not m['points'] or point[0] > m['points'][-1][0]:
                m['points'].append(point)

    def since(self, after):
        """Points newer than ``after`` for every asset's current market."""
        with self.lock:
            return {a: dict(ticker=m['ticker'], close=m['close'], points=[p for p in m['points'] if p[0] > after])
                    for a, m in self.markets.items()}
