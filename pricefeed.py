"""1-second spot price feed for the dashboard charts, and for the fair-value model when enabled.

One background thread polls Coinbase's public ticker for every watched asset once per
second, in parallel, and keeps the last ``KEEP_SECONDS`` of samples in memory. All
browser tabs read from this buffer, so the request rate to Coinbase stays fixed at one
request per asset per second (Coinbase public limit: 10 req/s per IP, bursts to 15).
"""
import json
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

COINBASE = 'https://api.exchange.coinbase.com'
KEEP_SECONDS = 1200          # 20 minutes: covers a full 15-minute market plus margin


def fetch_ticker(product, timeout=2.5):
    req = urllib.request.Request(f'{COINBASE}/products/{product}/ticker',
                                 headers={'Accept': 'application/json', 'User-Agent': 'KalshiDesk/1.2'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        price = float(json.load(r)['price'])
    if not price > 0:
        raise ValueError('Non-positive price')
    return price


class TickFeed:
    def __init__(self, products, assets_fn, fetch=fetch_ticker, clock=time.time):
        self.products = products            # asset -> Coinbase product id
        self.assets_fn = assets_fn          # callable returning the assets to poll right now
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.ticks = {}                     # asset -> deque[(second, price)]
        self.errors = {}                    # asset -> text
        self.pool = ThreadPoolExecutor(max_workers=len(products) or 1)

    def poll_once(self):
        assets = [a for a in self.assets_fn() if a in self.products]
        second = int(self.clock())
        futures = {a: self.pool.submit(self.fetch, self.products[a]) for a in assets}
        for a, fut in futures.items():
            try:
                price = fut.result()
            except Exception as e:
                with self.lock:
                    self.errors[a] = 'Live price unavailable (' + type(e).__name__ + ')'
                continue
            with self.lock:
                buf = self.ticks.setdefault(a, deque(maxlen=KEEP_SECONDS))
                if buf and buf[-1][0] >= second:
                    buf[-1] = (buf[-1][0], price)        # a slow reply never goes backwards in time
                else:
                    buf.append((second, price))
                self.errors.pop(a, None)

    def since(self, after):
        """Samples newer than ``after`` (unix seconds) for every asset, plus current errors."""
        with self.lock:
            return dict(ticks={a: [[t, p] for t, p in buf if t > after] for a, buf in self.ticks.items()},
                        errors=dict(self.errors), now=self.clock())

    def series(self, asset):
        """All buffered samples for ``asset`` as [(unix second, price)], oldest first."""
        with self.lock:
            return list(self.ticks.get(asset, ()))

    def run(self):
        while True:
            started = self.clock()
            try:
                self.poll_once()
            except Exception:
                pass                                     # per-asset errors are recorded in poll_once
            # Wake on the next whole second so samples stay evenly spaced.
            time.sleep(max(0.05, 1 - (self.clock() - int(started)) % 1))
