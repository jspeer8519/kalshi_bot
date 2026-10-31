"""Kalshi Pro data feed: CF Benchmarks index and trades over Kalshi's WebSocket. Fully faked here."""
import asyncio
import base64
import json
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import indexfeed
from indexfeed import IndexFeed, sign_headers, trade_row


def value_5hz(index_id, value, ms):
    return dict(type='cfbenchmarks_value_5hz', msg=dict(index_id=index_id, value_usd=f'{value:.8f}', source_ts_ms=ms))


def value_1s(index_id, value, ms, avg=None):
    vendor = json.dumps(dict(type='value', time=ms, id=index_id, value=str(value)))
    m = dict(type='cfbenchmarks_value', msg=dict(index_id=index_id, data=vendor))
    if avg is not None:
        m['msg']['avg_60s_data'] = dict(value=str(avg), window_size=60, window_start_ts_ms=ms - 60000, window_end_ts_exclusive=ms)
    return m


def ws_trade(ticker, price, count, side, ms, tid):
    return dict(type='trade', msg=dict(trade_id=tid, market_ticker=ticker, yes_price_dollars=f'{price:.4f}', count_fp=f'{count:.2f}',
                                       taker_side=side, ts=ms // 1000, ts_ms=ms))


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.f = IndexFeed(lambda: None, lambda: [])

    def test_index_ids_match_the_contract_rules(self):
        self.assertEqual(indexfeed.INDEX_IDS['BTC'], 'BRTI')
        self.assertEqual(set(indexfeed.INDEX_IDS.values()) - set(indexfeed.FIVE_HZ), {'HYPEUSD_RTI', 'BNBUSD_RTI'})

    def test_5hz_values_feed_the_chart_and_1s_values_feed_settlement_series(self):
        for i, v in enumerate((100.0, 100.5, 100.25)):
            self.f.handle(value_5hz('BRTI', v, 1_000_000 + i * 200))
        self.f.handle(value_1s('BRTI', 100.3, 1_001_000, avg=100.1))
        out = self.f.since(0)['index']['BTC']
        self.assertEqual(out['points'], [[1000.0, 100.0], [1000.2, 100.5], [1000.4, 100.25]])   # 1/s value not duplicated into 5 Hz points
        self.assertEqual(out['avg60']['value'], 100.1); self.assertEqual(out['id'], 'BRTI')
        self.assertEqual(self.f.series('BTC'), [(1001, 100.3)])
        self.assertEqual(out['avg'], [[1001, 100.1]])                                            # Kalshi's chart line
        self.assertEqual(self.f.since(1001)['index']['BTC']['avg'], [])

    def test_coins_without_5hz_use_the_1s_values_on_the_chart(self):
        self.f.handle(value_1s('HYPEUSD_RTI', 87.6, 2_000_000))
        self.assertEqual(self.f.since(0)['index']['HYPE']['points'], [[2000.0, 87.6]])
        self.assertEqual(self.f.series('HYPE'), [(2000, 87.6)])

    def test_out_of_order_bad_and_unknown_values_are_ignored(self):
        self.f.handle(value_5hz('BRTI', 100, 1_000_400)); self.f.handle(value_5hz('BRTI', 99, 1_000_200))
        self.f.handle(value_5hz('NOPE_RTI', 1, 1_000_600)); self.f.handle(value_5hz('BRTI', 0, 1_000_800))
        self.f.handle(dict(type='cfbenchmarks_value_5hz', msg=dict(index_id='BRTI', value_usd='x', source_ts_ms=1_001_000)))
        self.assertEqual(self.f.since(0)['index']['BTC']['points'], [[1000.4, 100.0]])

    def test_old_points_age_out(self):
        self.f.handle(value_5hz('BRTI', 1, 1_000_000)); self.f.handle(value_5hz('BRTI', 2, 1_000_000 + (indexfeed.KEEP_SECONDS + 5) * 1000))
        self.assertEqual(len(self.f.since(0)['index']['BTC']['points']), 1)

    def test_since_returns_only_newer_points_and_requested_trades(self):
        self.f.handle(value_5hz('BRTI', 1, 1_000_000)); self.f.handle(value_5hz('BRTI', 2, 1_002_000))
        self.f.handle(ws_trade('T1', .82, 3, 'yes', 1_001_500, 'a')); self.f.handle(ws_trade('T2', .5, 1, 'no', 1_001_500, 'b'))
        out = self.f.since(1001, ['T1'])
        self.assertEqual(out['index']['BTC']['points'], [[1002.0, 2.0]])
        self.assertEqual(out['trades'], {'T1': [[1001.5, .82, 3.0, 'yes']]})

    def test_trades_from_websocket_and_rest_are_stored_once(self):
        rest = dict(trade_id='a', ticker='T1', yes_price_dollars='0.8200', count_fp='3.00', taker_side='yes', created_time='2026-10-31T08:53:14.500Z')
        self.f.handle(ws_trade('T1', .82, 3, 'yes', 1_001_500, 'a'))
        self.f.add_trades('T1', [rest, dict(rest, trade_id='c')])
        self.assertEqual(len(self.f.since(0, ['T1'])['trades']['T1']), 2)
        self.assertEqual(trade_row(rest)[:4], [1793436794.5, .82, 3.0, 'yes'])


class SigningTests(unittest.TestCase):
    def test_signature_covers_timestamp_get_and_ws_path(self):
        for key in (ed25519.Ed25519PrivateKey.generate(), rsa.generate_private_key(public_exponent=65537, key_size=2048)):
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            h = sign_headers(dict(key_id='kid', pem=pem), 1793436000000)
            self.assertEqual((h['KALSHI-ACCESS-KEY'], h['KALSHI-ACCESS-TIMESTAMP']), ('kid', '1793436000000'))
            msg, sig = b'1793436000000GET/trade-api/ws/v2', base64.b64decode(h['KALSHI-ACCESS-SIGNATURE'])
            if isinstance(key, ed25519.Ed25519PrivateKey): key.public_key().verify(sig, msg)
            else: key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


class FakeSocket:
    def __init__(self, incoming):
        self.incoming, self.sent = list(incoming), []

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self.incoming:
            raise ConnectionError('closed')
        item = self.incoming.pop(0)
        if callable(item): item = item(self)
        return json.dumps(item)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class SessionTests(unittest.TestCase):
    def run_session(self, incoming, tickers, creds=True):
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sock, seen = FakeSocket(incoming), {}
        def connect(url, additional_headers, open_timeout):
            seen.update(url=url, headers=additional_headers); return sock
        f = IndexFeed(lambda: dict(key_id='kid', pem=pem) if creds else None, tickers)
        with self.assertRaises((ConnectionError, PermissionError)):
            asyncio.run(f._session(connect))
        return f, sock, seen

    def test_subscribes_to_both_index_channels_and_trades(self):
        f, sock, seen = self.run_session([value_5hz('BRTI', 77164.1, 1_000_000)], lambda: ['KXBTC15M-A', 'KXETH15M-A'])
        self.assertEqual(seen['url'], 'wss://external-api-ws.kalshi.com/trade-api/ws/v2')
        self.assertEqual(set(seen['headers']), {'KALSHI-ACCESS-KEY', 'KALSHI-ACCESS-TIMESTAMP', 'KALSHI-ACCESS-SIGNATURE'})
        cmds = [(c['cmd'], c['params'].get('channels'), c['params'].get('index_ids'), c['params'].get('market_tickers')) for c in sock.sent]
        self.assertEqual(cmds[0], ('subscribe', ['cfbenchmarks_value'], list(indexfeed.INDEX_IDS.values()), None))
        self.assertEqual(cmds[1], ('subscribe', ['cfbenchmarks_value_5hz'], list(indexfeed.FIVE_HZ), None))
        self.assertEqual(cmds[2], ('subscribe', ['trade'], None, ['KXBTC15M-A', 'KXETH15M-A']))
        self.assertEqual(f.since(0)['index']['BTC']['points'], [[1000.0, 77164.1]])

    def test_resubscribes_trades_when_markets_roll_over(self):
        tickers = [['KXBTC15M-A']]
        def roll(sock):
            tickers[0] = ['KXBTC15M-B']; return dict(type='ok')
        incoming = [lambda s: dict(type='subscribed', id=3, msg=dict(channel='trade', sid=7)), roll, dict(type='ok')]
        f, sock, _ = self.run_session(incoming, lambda: tickers[0])
        tail = [(c['cmd'], c['params']) for c in sock.sent[3:]]
        self.assertEqual(tail[0], ('unsubscribe', dict(sids=[7])))
        self.assertEqual(tail[1], ('subscribe', dict(channels=['trade'], market_tickers=['KXBTC15M-B'])))

    def test_errors_are_reported_without_secrets(self):
        f, _, _ = self.run_session([dict(type='error', msg=dict(code=2, msg='Params required'))], lambda: [])
        self.assertEqual(f.error, 'Kalshi: Params required')
        f, _, _ = self.run_session([], lambda: [], creds=False)
        self.assertEqual(f.status, 'starting')

    def test_run_marks_missing_credentials_and_never_exposes_keys(self):
        f = IndexFeed(lambda: None, lambda: [])
        import time as _t
        orig = _t.sleep
        def stop(_):
            raise KeyboardInterrupt
        indexfeed.time.sleep = stop
        try:
            with self.assertRaises(KeyboardInterrupt): f.run(connect=lambda *a, **k: None)
        finally:
            indexfeed.time.sleep = orig
        self.assertEqual((f.status, f.error), ('no credentials', 'No Kalshi credentials saved'))
