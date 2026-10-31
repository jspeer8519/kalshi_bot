"""Kalshi Pro data: the CF Benchmarks settlement index and every public trade, from Kalshi's
official WebSocket API (wss://external-api-ws.kalshi.com/trade-api/ws/v2). Read-only: it only
subscribes to market data and never sends orders.

Channels (docs.kalshi.com/websockets):
* ``cfbenchmarks_value_5hz``: index values up to 5 per second (BRTI, ETHUSD_RTI, SOLUSD_RTI,
  XRPUSD_RTI, DOGEUSD_RTI) -> the 1-second candles on the chart.
* ``cfbenchmarks_value``: the once-per-second index values the markets settle on, for all seven
  coins, with Kalshi's running 60-second average (``avg_60s_data``). That average is the line
  Kalshi's own market chart draws ("NOW"), and its value at close is the settlement value.
* ``trade``: every public trade in the markets being watched.

The connection is signed with the saved Kalshi API key, loaded by the app's existing credential
loader and signed the same way as live.py (timestamp + "GET" + "/trade-api/ws/v2"). Keys,
signatures and headers are never logged or stored."""
import asyncio
import base64
import json
import threading
import time
from collections import deque

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

WS_URL = 'wss://external-api-ws.kalshi.com/trade-api/ws/v2'
WS_PATH = '/trade-api/ws/v2'
INDEX_IDS = {'BTC': 'BRTI', 'ETH': 'ETHUSD_RTI', 'SOL': 'SOLUSD_RTI', 'XRP': 'XRPUSD_RTI',
             'DOGE': 'DOGEUSD_RTI', 'HYPE': 'HYPEUSD_RTI', 'BNB': 'BNBUSD_RTI'}   # each series' rulebook "Index"
FIVE_HZ = ('BRTI', 'ETHUSD_RTI', 'SOLUSD_RTI', 'XRPUSD_RTI', 'DOGEUSD_RTI')
ASSET_OF = {v: k for k, v in INDEX_IDS.items()}
KEEP_SECONDS = 1200              # a full 15-minute market plus the minute before it, with margin
KEEP_TRADES = 20000              # per market
KEEP_MARKETS = 14                # two 15-minute sessions of all seven coins


def sign_headers(creds, now_ms):
    key = serialization.load_pem_private_key(creds['pem'].encode(), password=None)
    message = (str(now_ms) + 'GET' + WS_PATH).encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    return {'KALSHI-ACCESS-KEY': creds['key_id'], 'KALSHI-ACCESS-TIMESTAMP': str(now_ms),
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trade_row(t):
    """[unix seconds, yes price, contracts, taker side, trade id] from a WebSocket or REST trade."""
    ts = t.get('ts_ms')
    ts = ts / 1000 if ts else _num(t.get('ts'))
    if ts is None and t.get('created_time'):
        from settings import timestamp
        ts = timestamp(t['created_time'])
    return [ts, _num(t.get('yes_price_dollars')), _num(t.get('count_fp')), t.get('taker_side'), t.get('trade_id')]


class IndexFeed:
    def __init__(self, credential_loader, tickers_fn, clock=time.time):
        self.credential_loader = credential_loader
        self.tickers_fn = tickers_fn            # callable -> market tickers to receive trades for
        self.clock = clock
        self.lock = threading.Lock()
        self.fast = {}                          # asset -> deque[(unix seconds, value)] at up to 5 Hz
        self.official = {}                      # asset -> deque[(unix second, value)] once per second
        self.avg60 = {}                         # asset -> Kalshi's latest running 60-second average
        self.avg_hist = {}                      # asset -> deque[(unix seconds, 60-s average)]: Kalshi's chart line
        self.trades = {}                        # ticker -> deque[trade_row]
        self.trade_ids = {}                     # ticker -> set of trade ids already stored
        self.status, self.error = 'starting', None

    # ---- messages ---------------------------------------------------------------------
    def handle(self, m):
        kind, msg = m.get('type'), m.get('msg') or {}
        if kind in ('cfbenchmarks_value', 'cfbenchmarks_value_5hz'):
            try:
                vendor = json.loads(msg.get('data') or '{}')
            except ValueError:
                vendor = {}
            index_id = msg.get('index_id') or vendor.get('id')
            asset = ASSET_OF.get(index_id)
            value = _num(msg.get('value_usd')) if msg.get('value_usd') is not None else _num(vendor.get('value'))
            ts_ms = msg.get('source_ts_ms') or vendor.get('time')
            if not asset or value is None or not value > 0 or not ts_ms:
                return
            t = ts_ms / 1000
            with self.lock:
                if kind == 'cfbenchmarks_value':
                    self._append(self.official, asset, (int(round(t)), value))
                    a = msg.get('avg_60s_data') or {}
                    if _num(a.get('value')):
                        self.avg60[asset] = dict(value=_num(a['value']), n=a.get('window_size'),
                                                 start=(a.get('window_start_ts_ms') or 0) / 1000,
                                                 end=(a.get('window_end_ts_exclusive') or 0) / 1000)
                        self._append(self.avg_hist, asset, (int(round(t)), _num(a['value'])))
                    if index_id in FIVE_HZ:
                        return
                self._append(self.fast, asset, (t, value))
        elif kind == 'trade':
            self.add_trades(msg.get('market_ticker'), [msg])

    def _append(self, store, asset, point):
        buf = store.setdefault(asset, deque())
        if buf and point[0] <= buf[-1][0]:
            if point[0] == buf[-1][0]:
                buf[-1] = point
            return
        buf.append(point)
        while buf and buf[0][0] < point[0] - KEEP_SECONDS:
            buf.popleft()

    def add_trades(self, ticker, trades):
        """Store trades for ``ticker`` (from the WebSocket or the REST backfill), once each."""
        if not ticker:
            return
        with self.lock:
            buf = self.trades.setdefault(ticker, deque(maxlen=KEEP_TRADES))
            ids = self.trade_ids.setdefault(ticker, set())
            for t in trades:
                row = trade_row(t)
                if row[0] is None or row[1] is None or row[2] is None or row[4] in ids:
                    continue
                ids.add(row[4]); buf.append(row)
            if len(self.trades) > KEEP_MARKETS:             # only the current and previous markets are charted
                for old in list(self.trades)[:-KEEP_MARKETS]:
                    self.trades.pop(old, None); self.trade_ids.pop(old, None)

    # ---- reads --------------------------------------------------------------------------
    def series(self, asset):
        """Official once-per-second index values [(unix second, value)], oldest first."""
        with self.lock:
            return list(self.official.get(asset, ()))

    def since(self, after, tickers=()):
        with self.lock:
            index = {a: dict(id=INDEX_IDS[a], points=[[round(t, 3), v] for t, v in self.fast.get(a, ()) if t > after],
                             avg=[[t, v] for t, v in self.avg_hist.get(a, ()) if t > after], avg60=self.avg60.get(a))
                     for a in set(self.fast) | set(self.avg_hist)}
            trades = {t: [r[:4] for r in self.trades.get(t, ()) if r[0] > after] for t in tickers}
            return dict(index=index, trades=trades, status=self.status, error=self.error)

    # ---- connection ---------------------------------------------------------------------
    async def _session(self, connect):
        creds = self.credential_loader()
        if not creds:
            raise PermissionError('No Kalshi credentials saved')
        async with connect(WS_URL, additional_headers=sign_headers(creds, int(self.clock() * 1000)), open_timeout=10) as ws:
            n = 0

            async def send(cmd, params):
                nonlocal n
                n += 1
                await ws.send(json.dumps(dict(id=n, cmd=cmd, params=params)))
                return n
            await send('subscribe', dict(channels=['cfbenchmarks_value'], index_ids=list(INDEX_IDS.values())))
            await send('subscribe', dict(channels=['cfbenchmarks_value_5hz'], index_ids=list(FIVE_HZ)))
            trade_cmd, trade_sid, tickers = None, None, []
            with self.lock:
                self.status, self.error = 'connected', None
            while True:
                wanted = sorted(set(self.tickers_fn()))
                if wanted != tickers:                       # the 15-minute markets rolled over
                    if trade_sid is not None:
                        await send('unsubscribe', dict(sids=[trade_sid]))
                    trade_cmd, trade_sid, tickers = (await send('subscribe', dict(channels=['trade'], market_tickers=wanted)) if wanted else None), None, wanted
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 1))
                except asyncio.TimeoutError:
                    continue
                if m.get('type') == 'subscribed' and m.get('id') == trade_cmd:
                    trade_sid = (m.get('msg') or {}).get('sid')
                elif m.get('type') == 'error':
                    with self.lock:
                        self.error = 'Kalshi: ' + str((m.get('msg') or {}).get('msg', 'error'))[:120]
                else:
                    self.handle(m)

    def run(self, connect=None):
        """Keep a connection open forever, reconnecting with back-off."""
        if connect is None:
            import websockets
            connect = websockets.connect
        delay = 2
        while True:
            try:
                asyncio.run(self._session(connect))
                delay = 2
            except PermissionError as e:
                with self.lock:
                    self.status, self.error = 'no credentials', str(e)
                delay = 60
            except Exception as e:            # network or protocol: never include headers in the message
                with self.lock:
                    self.status, self.error = 'reconnecting', 'Index feed unavailable (' + type(e).__name__ + ')'
                delay = min(delay * 2, 60)
            time.sleep(delay)
