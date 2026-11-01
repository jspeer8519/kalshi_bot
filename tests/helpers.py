"""Shared fixtures for the feature tests. Nothing here touches the network."""
import json
import tempfile
import unittest
from pathlib import Path

from engine import Engine

NOW = 1800000000
CLOSE = NOW + 120


def market(asset='BTC', **kw):
    base = dict(asset=asset, ticker=f'KX{asset}15M-T1', status='active', result='', close=CLOSE, fetched=NOW, strike=None,
                yes_ask=.85, no_ask=.17, yes_bid=.83, no_bid=.15, yes_ask_size=10, no_ask_size=10, yes_bid_size=10, no_bid_size=10)
    return dict(base, **kw)


class FakeExecutor:
    """Stands in for live.LiveExecutor. Records calls; never sends anything."""
    def __init__(self):
        self.held, self.cash, self.calls = 0.0, 50.0, []
        self.open_result = self.close_result = None

    def position(self, ticker):
        self.calls.append(('position', ticker)); return self.held

    def balance(self):
        return self.cash

    def open(self, ticker, side, price, count):
        self.calls.append(('open', ticker, side, price, count))
        return self.open_result if self.open_result is not None else dict(size=count, price=price, fee=.01, order_id='o-'+ticker, client_order_id='c')

    def close(self, ticker, side, bid, count):
        self.calls.append(('close', ticker, side, bid, count))
        return self.close_result if self.close_result is not None else dict(size=count, price=bid, fee=.01, order_id='x-'+ticker, client_order_id='c')


class EngineCase(unittest.TestCase):
    executor = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'p.db')
        self.logp = str(Path(self.temp.name) / 'log.jsonl')
        self.spot = {}
        self.e = self.make()
        self.now = NOW

    def make(self, **kw):
        return Engine(self.path, live_enabled=True, executor=self.executor, log_path=self.logp, spot_fn=lambda a: self.spot.get(a, []), **kw)

    def tearDown(self):
        self.e.db.close(); self.temp.cleanup()

    def cfg(self, **kw):
        self.e.s['config'].update(kw)

    def events(self):
        return [json.loads(x) for x in Path(self.logp).read_text().splitlines()] if Path(self.logp).exists() else []

    def messages(self):
        return [ev['message'] for ev in self.e.s['events']]
