"""Executors: the only part of the engine that differs between modes.

* PAPER  -> SimExecutor('paper'): simulated fills against the real Kalshi book. Realism (whole
  contracts, depth walking, latency) follows the paper_* settings; all off = legacy model.
* SHADOW -> SimExecutor('shadow'): real book, simulated fill, never sends an order. Always walks
  depth with whole contracts, plus ``shadow_latency_ms``. Separate ledger; paper cash untouched.
* LIVE_SMALL -> LiveSmallExecutor: real IOC orders through live.LiveExecutor, exactly one
  contract per entry.

Every executor receives the same order built from the same decision. ``enter``/``exit`` return
(status, fill): FILLED with dict(size, price, fee[, order_id]); PENDING (simulated latency: the
engine fills it on the first snapshot fetched at or after the due time); NOFILL (order went out,
nothing filled: the market is used); SKIPPED (nothing was sent)."""
import time

from fills import simulate
from live import LiveAmbiguous, LiveError
from settings import fee

FILLED, PENDING, NOFILL, SKIPPED = 'filled', 'pending', 'nofill', 'skipped'


class SimExecutor:
    sends_orders = False

    def __init__(self, mode):
        self.mode = mode

    def params(self, c):
        """(walk depth, whole contracts only, latency seconds) for these settings."""
        if self.mode == 'shadow':
            return True, True, c['shadow_latency_ms'] / 1000
        return c['paper_depth_fill'], c['paper_whole_contracts'], c['paper_latency_ms'] / 1000

    def effective_count(self, n):
        return n

    def fill(self, c, m, order):
        """Fill ``order`` against snapshot ``m`` right now, or None."""
        depth, whole, _ = self.params(c)
        n, avg, worst = simulate(m, order['side'], order['intent'], order['count'], order['price'], depth, whole)
        if n <= 0:
            return None
        return dict(size=n, price=avg, fee=fee(avg, n), worst=worst)

    def _submit(self, eng, asset, m, order, now):
        c = eng.s['config']
        latency = self.params(c)[2]
        if latency > 0:
            eng.queue_sim(dict(order, model=self.mode, asset=asset, submitted=now, due=now + latency))
            return PENDING, None
        f = self.fill(c, m, order)
        return (FILLED, f) if f else (NOFILL, None)

    def enter(self, eng, asset, m, order, now):
        if self.mode == 'paper':
            cost = round(order['price']*order['count'] + fee(order['price'], order['count']), 6)
            if cost > eng.s['cash']:
                eng._issue(asset, f'PAPER {asset} entry skipped: cost ${cost:.2f} exceeds paper cash.')
                return SKIPPED, None
        return self._submit(eng, asset, m, order, now)

    def exit(self, eng, asset, p, m, order, now):
        return self._submit(eng, asset, m, order, now)

    def post_fill(self, eng):
        pass


class LiveSmallExecutor:
    """Real orders on Kalshi, one contract per entry. Every order is IOC; see live.py."""
    sends_orders = True
    mode = 'live'
    CONTRACTS = 1

    def __init__(self, executor):
        self.x = executor

    def effective_count(self, n):
        return min(n, self.CONTRACTS)

    MAX_QUOTE_AGE = 10    # seconds, as for every decision (strategy.FRESH)

    @staticmethod
    def _inflight(eng, asset, ticker, side, intent, order):
        """Persist an unresolved-order marker BEFORE sending. If the app restarts while it is set, the
        engine halts live trading until a human has checked the account (see Engine.__init__)."""
        eng.s['live_inflight'] = dict(asset=asset, ticker=ticker, side=side, intent=intent, decision=order['decision'], at=time.time())
        eng.save()

    @staticmethod
    def _resolved(eng):
        eng.s['live_inflight'] = None
        eng.save()

    def enter(self, eng, asset, m, order, now):
        ticker, side, price, count = m['ticker'], order['side'], order['price'], order['count']
        started = time.time()
        if eng.s['live_halt']:
            return SKIPPED, None
        if not eng.log.healthy:
            eng.halt(f'The audit log could not be written ({eng.log.last_error}). Live orders stop until the disk/log problem is fixed.')
            return SKIPPED, None
        if count > eng.live_max_contracts:
            eng.running = False
            eng.record(f'LIVE entry blocked: {count} contracts exceeds live cap {eng.live_max_contracts}. Entries paused.')
            return SKIPPED, None
        held = self.x.position(ticker)
        if held is None:
            eng._issue(asset, f'LIVE {asset} entry skipped: account position check unavailable; will retry.')
            return SKIPPED, None
        if held != 0:
            eng.s['seen'].append(ticker); eng.changed()
            eng.record(f'LIVE {asset} entry skipped: account already holds {held:g} on {ticker}.')
            eng.log.write('entry_skipped', mode='live', asset=asset, ticker=ticker, reason='existing_position', position=held)
            return SKIPPED, None
        balance = self.x.balance()
        need = round(price*count + fee(price, count), 6)
        if balance is None:
            eng._issue(asset, f'LIVE {asset} entry skipped: balance unavailable; will retry.')
            return SKIPPED, None
        eng.s['live_balance'] = balance
        if balance < need:
            eng.s['seen'].append(ticker); eng.changed()
            eng.record(f'LIVE {asset} entry skipped: balance ${balance:.2f} below estimated cost ${need:.2f}.')
            eng.log.write('entry_skipped', mode='live', asset=asset, ticker=ticker, reason='insufficient_balance', balance=balance, need=need)
            return SKIPPED, None
        # Mark the market as used and persist BEFORE sending, so a crash can never re-enter it,
        # together with the in-flight marker that halts live trading if we restart before resolving it.
        eng.s['seen'].append(ticker)
        self._inflight(eng, asset, ticker, side, 'entry', order)
        eng.log.write('order_submit', mode='live', intent='entry', asset=asset, ticker=ticker, side=side, price=price, count=count, decision=order['decision'])
        if self._stale(eng, asset, m, now, started, 'entry'):
            eng.s['seen'].remove(ticker); self._resolved(eng)   # nothing was sent: the market may be re-decided on fresh data
            return SKIPPED, None
        try:
            fill = self.x.open(ticker, side, price, count)
        except LiveAmbiguous as e:
            eng.log.write('order_result', mode='live', intent='entry', asset=asset, outcome='ambiguous', error=str(e))
            eng.halt(str(e)); self._resolved(eng); return NOFILL, None
        except LiveError as e:
            eng.log.write('order_result', mode='live', intent='entry', asset=asset, outcome='rejected', error=str(e))
            eng.record(f'LIVE {asset} entry rejected: {e}'); self._resolved(eng); return NOFILL, None
        if not fill:
            eng.log.write('order_result', mode='live', intent='entry', asset=asset, outcome='no_fill', ticker=ticker)
            eng.record(f'LIVE {asset} entry: no fill at {price*100:.1f}¢ (IOC). Market skipped.'); self._resolved(eng); return NOFILL, None
        eng.log.write('order_result', mode='live', intent='entry', asset=asset, outcome='filled', **fill)
        return FILLED, fill                         # the engine records the position, then clears the marker

    def _stale(self, eng, asset, m, now, started, intent):
        """Last check before sending: the account checks, saves and log writes since the decision can take
        seconds, so the quote must still be fresh (and the market open) at this exact moment."""
        t = now + (time.time() - started)
        if t - m['fetched'] > self.MAX_QUOTE_AGE or t >= m['close']:
            eng._issue(asset, f'LIVE {asset} {intent} not sent: quote is {t - m["fetched"]:.0f} s old by send time; will re-decide on fresh data.')
            eng.log.write('order_not_sent', mode='live', intent=intent, asset=asset, ticker=m['ticker'], quote_age=round(t - m['fetched'], 3))
            return True
        return False

    def exit(self, eng, asset, p, m, order, now):
        started = time.time()
        reason = order['reason']
        if eng.s['live_halt']:
            eng._issue(asset, f'LIVE {asset} exit signal ({reason}) ignored while halted.')
            return SKIPPED, None
        # Sell what the observed book can absorb above the floor, limit at the worst level needed.
        bid, count = order['worst'], order['decided']
        self._inflight(eng, asset, p['ticker'], p['side'], 'exit', order)
        eng.log.write('order_submit', mode='live', intent='exit', reason=reason, asset=asset, ticker=p['ticker'], side=p['side'], price=bid, count=count, decision=order['decision'])
        if self._stale(eng, asset, m, now, started, 'exit'):
            self._resolved(eng)
            return SKIPPED, None
        try:
            fill = self.x.close(p['ticker'], p['side'], bid, count)
        except LiveAmbiguous as e:
            eng.log.write('order_result', mode='live', intent='exit', asset=asset, outcome='ambiguous', error=str(e))
            eng.halt(str(e)); self._resolved(eng); return NOFILL, None
        except LiveError as e:
            eng.log.write('order_result', mode='live', intent='exit', asset=asset, outcome='rejected', error=str(e))
            eng._issue(asset, f'LIVE {asset} exit not filled ({reason}): {e}'); self._resolved(eng); return NOFILL, None
        if not fill:
            eng.log.write('order_result', mode='live', intent='exit', asset=asset, outcome='no_fill', ticker=p['ticker'])
            eng._issue(asset, f'LIVE {asset} exit ({reason}) found no fill at {bid*100:.1f}¢; will retry.'); self._resolved(eng); return NOFILL, None
        eng.log.write('order_result', mode='live', intent='exit', asset=asset, outcome='filled', **fill)
        return FILLED, fill                         # the engine records the sale, then clears the marker

    def post_fill(self, eng):
        eng._refresh_live_balance()
