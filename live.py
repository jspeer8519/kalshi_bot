"""Live Kalshi execution. Used only when the dashboard is switched to LIVE.

Every order is an immediate-or-cancel (IOC) limit order at the quoted price, so
nothing is ever left resting on the book. Outcomes split into three cases:

* ``None`` returned        -> order accepted, nothing filled (safe, no position).
* ``OrderRejected`` raised -> Kalshi definitively refused the order (safe).
* ``LiveAmbiguous`` raised -> outcome unknown (network error, 5xx, malformed
  response, position mismatch). The engine halts all live activity until a
  human checks the account in Kalshi.

Reference: POST /portfolio/events/orders (Create Order V2). ``side`` quotes the
YES leg only: ``bid`` buys YES, ``ask`` sells YES (economically buys NO at 1-p).
"""
import base64
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

API = 'https://external-api.kalshi.com/trade-api/v2'
API_PREFIX = '/trade-api/v2'


class LiveError(Exception):
    """Base class for live execution failures."""


class OrderRejected(LiveError):
    """Kalshi refused the order or a precondition failed. No order exists."""


class LiveAmbiguous(LiveError):
    """The order may or may not exist. Live activity must halt."""


class KalshiClient:
    """Signed REST client. Credentials are loaded per call and never logged."""

    def __init__(self, credential_loader, base=API, timeout=8):
        self.credential_loader = credential_loader
        self.base = base
        self.timeout = timeout

    def _headers(self, method, path):
        creds = self.credential_loader()
        if not creds:
            raise OrderRejected('No Kalshi credentials configured')
        key = serialization.load_pem_private_key(creds['pem'].encode(), password=None)
        stamp = str(int(time.time() * 1000))
        message = (stamp + method + API_PREFIX + path.split('?', 1)[0]).encode()
        if isinstance(key, ed25519.Ed25519PrivateKey):
            signature = key.sign(message)
        else:
            signature = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return {'KALSHI-ACCESS-KEY': creds['key_id'], 'KALSHI-ACCESS-TIMESTAMP': stamp,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}

    def request(self, method, path, body=None):
        """Return (status, parsed_json). Raises urllib/OSError on transport failure."""
        headers = {'Accept': 'application/json', 'User-Agent': 'KalshiDesk/1.1', **self._headers(method, path)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            return e.code, parsed


def _num(value):
    """A finite number, or None (missing, malformed, NaN and infinity all count as unknown)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _error_text(status, body):
    err = body.get('error', body) if isinstance(body, dict) else {}
    if not isinstance(err, dict):
        err = {}
    code, msg = err.get('code', ''), err.get('message', '')
    return f'HTTP {status}' + (f' {code}' if code else '') + (f': {msg}' if msg else '')


class LiveExecutor:
    def __init__(self, client, verify_attempts=3, verify_delay=1.0, sleep=time.sleep):
        self.client = client
        self.verify_attempts = verify_attempts
        self.verify_delay = verify_delay
        self.sleep = sleep

    # ---- read-only account checks -------------------------------------------------
    def balance(self):
        """Available cash in dollars, or None if it cannot be read."""
        try:
            status, body = self.client.request('GET', '/portfolio/balance')
        except (OSError, ValueError, LiveError):
            return None
        if status != 200:
            return None
        dollars = _num(body.get('balance_dollars'))
        if dollars is not None:
            return dollars
        cents = _num(body.get('balance'))
        return None if cents is None else cents / 100

    def position(self, ticker):
        """Signed YES position (+YES / -NO) for ticker, 0 if flat, None if unknown."""
        try:
            status, body = self.client.request('GET', '/portfolio/positions?ticker=' + urllib.parse.quote(ticker, safe=''))
        except (OSError, ValueError, LiveError):
            return None
        # Fail closed: a reply without the positions list is "unknown", never "flat".
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get('market_positions'), list):
            return None
        for row in body['market_positions']:
            if not isinstance(row, dict) or not row.get('ticker'):
                return None                       # a malformed row could be our holding: unknown, not flat
            if row.get('ticker') == ticker:
                qty = _num(row.get('position_fp', row.get('position')))
                return qty if qty is not None else None
        return 0.0

    # ---- orders -------------------------------------------------------------------
    def _submit(self, ticker, yes_side, yes_price, count, reduce_only):
        client_order_id = str(uuid.uuid4())
        payload = {
            'ticker': ticker,
            'client_order_id': client_order_id,
            'side': yes_side,
            'count': f'{count:.2f}',
            'price': f'{yes_price:.4f}',
            'time_in_force': 'immediate_or_cancel',
            'self_trade_prevention_type': 'taker_at_cross',
            'reduce_only': reduce_only,
        }
        try:
            status, body = self.client.request('POST', '/portfolio/events/orders', payload)
        except OrderRejected:
            raise
        except Exception as e:  # timeout/connection reset: Kalshi may have accepted it
            raise LiveAmbiguous(f'No response to order {client_order_id} ({type(e).__name__})') from e
        if status >= 500:
            raise LiveAmbiguous(f'Order {client_order_id} outcome unknown: ' + _error_text(status, body))
        if status not in (200, 201):
            raise OrderRejected('Order rejected: ' + _error_text(status, body))
        if not isinstance(body, dict):
            raise LiveAmbiguous(f'Order {client_order_id} accepted with an unreadable response')
        order_id = body.get('order_id')
        filled = _num(body.get('fill_count'))
        if not order_id or filled is None or not 0 <= filled <= count + 1e-9:
            raise LiveAmbiguous(f'Order {client_order_id} accepted with an incomplete or impossible response')
        result = dict(order_id=order_id, client_order_id=client_order_id, payload=payload, filled=filled)
        if filled > 0:
            avg = _num(body.get('average_fill_price'))
            fee_each = _num(body.get('average_fee_paid'))
            if avg is None or fee_each is None or not 0 < avg < 1 or fee_each < 0:
                raise LiveAmbiguous(f'Order {order_id} filled but price/fee missing or invalid in the response')
            result.update(avg_yes_price=avg, fee=round(fee_each * filled, 6))
        return result

    def open(self, ticker, side, price, count):
        """Buy ``count`` contracts of ``side`` ('yes'/'no') at up to ``price``.

        Returns None if nothing filled, else dict(size, price, fee, order_id, ...).
        ``price`` and returned prices are for the contract held (YES or NO).
        """
        yes_side, yes_price = ('bid', price) if side == 'yes' else ('ask', 1 - price)
        r = self._submit(ticker, yes_side, yes_price, count, reduce_only=False)
        if r['filled'] <= 0:
            return None
        expected = r['filled'] if side == 'yes' else -r['filled']
        held = None
        for attempt in range(self.verify_attempts):  # positions can lag the matching engine briefly
            held = self.position(ticker)
            if held is not None and abs(held - expected) <= 1e-6:
                break
            if attempt + 1 < self.verify_attempts:
                self.sleep(self.verify_delay)
        if held is not None and abs(held - expected) > 1e-6:
            raise LiveAmbiguous(f'Order {r["order_id"]} filled {r["filled"]} but account shows position {held} on {ticker}')
        contract_price = r['avg_yes_price'] if side == 'yes' else round(1 - r['avg_yes_price'], 6)
        return dict(size=r['filled'], price=contract_price, fee=r['fee'], order_id=r['order_id'],
                    client_order_id=r['client_order_id'], position_verified=held is not None)

    def close(self, ticker, side, bid, count):
        """Sell up to ``count`` held contracts of ``side`` at no less than ``bid`` (reduce-only)."""
        yes_side, yes_price = ('ask', bid) if side == 'yes' else ('bid', 1 - bid)
        r = self._submit(ticker, yes_side, yes_price, count, reduce_only=True)
        if r['filled'] <= 0:
            return None
        contract_price = r['avg_yes_price'] if side == 'yes' else round(1 - r['avg_yes_price'], 6)
        return dict(size=r['filled'], price=contract_price, fee=r['fee'], order_id=r['order_id'],
                    client_order_id=r['client_order_id'])
