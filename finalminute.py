"""Final-minute monitor (record only; never trades).

Kalshi settles on the simple average of the 60 one-second index values before close. Inside that
minute the values already published are fixed, so the bot can work out how likely each side is to
win from the known part plus a random walk for the seconds still to come, and compare that with
the asks on the book. With ``final_minute_log`` on, every market snapshot in a market's last 60
seconds is appended to ``final_minute.jsonl`` next to the state database, and once the market has
closed its outcome is computed from the same index values and appended too.
``finalminutereport.py`` turns the file into a report. Nothing here changes a decision or an order.

Model for the seconds still to come: zero-drift random walk from the latest price with the realized
per-second variance (fairvalue.variance_per_second). The sum of the next r values then has standard
deviation  price * sigma * sqrt(r(r+1)(2r+1)/6).  Only values at or before ``now`` are used."""
import json
import math
import os
import queue
import threading

from fairvalue import MAX_SPOT_AGE, WINDOW, _phi, variance_per_second

LOOKBACK = 900           # seconds of 1-second returns for the volatility estimate
OUTCOME_VALUES = 60      # all 60 settlement seconds are needed for a definitive outcome; fewer = provisional
MAX_STEP = 1.5           # seconds: the published settlement values so far must have no gap larger than this
OUTCOME_DELAY = 3        # seconds after close before the outcome is computed
OUTCOME_GIVE_UP = 300


def _settlement_seconds(ticks, close):
    """Index values in the settlement minute keyed by whole second (close-60 .. close-1)."""
    return {int(round(t)): p for t, p in ticks if close - WINDOW <= round(t) < close and p > 0}


def final_prob(ticks, strike, close, now, lookback=LOOKBACK):
    """P(the settlement average is at least ``strike``) inside the final minute, or None.
    dict(p_yes, known, remaining, partial_avg, price).

    The settlement seconds published so far must run without a gap from close-60 to the latest one;
    a missing past value is unknown history, not a future step, so any gap makes the estimate
    unavailable. The seconds after the latest published value are the random part."""
    if strike is None or not close - WINDOW <= now < close:
        return None
    pts = [(t, p) for t, p in ticks if t <= now and p > 0]
    if not pts or now - pts[-1][0] > MAX_SPOT_AGE:
        return None
    secs = _settlement_seconds(pts, close)
    if secs:
        last = max(secs)
        if len(secs) != last - (close - WINDOW) + 1:
            return None                                   # a past settlement second is missing
        r = close - 1 - last                              # settlement seconds still to come
    else:
        if now - (close - WINDOW) > MAX_STEP + 1:
            return None                                   # the minute has started but nothing was published
        r = WINDOW
    known = list(secs.values()); k = len(known); price = pts[-1][1]
    var = variance_per_second(ticks, now, lookback)
    if var is None:
        return None
    need = WINDOW * strike - sum(known) - r * price          # how far the sum must still move (positive = YES behind)
    sd = price * math.sqrt(var) * math.sqrt(r * (r + 1) * (2 * r + 1) / 6) if r else 0.0
    p = (1.0 if need <= 0 else 0.0) if sd <= 0 else 1 - _phi(need / sd)
    return dict(p_yes=round(p, 6), known=k, remaining=r, partial_avg=round(sum(known) / k, 8) if k else None, price=price)


def final_average(ticks, close):
    """Settlement value from recorded index values: mean over the distinct seconds in [close-60, close), with their count.
    Only a count of 60 is the complete settlement average."""
    secs = _settlement_seconds(ticks, close)
    return (sum(secs.values()) / len(secs), len(secs)) if secs else (None, 0)


class FinalMinute:
    """Mixed into Engine. ``self.fm_path`` and ``self.index_fn`` (settlement index only, never the Coinbase
    fallback, so outcomes and probabilities are computed on the values Kalshi settles on) are set by Engine.__init__."""

    def _fm_write(self, row):
        """Queue a row for the background writer: no file I/O happens inside the trading update."""
        q = self.__dict__.get('fm_queue')
        if q is None:
            q = self.fm_queue = queue.Queue(maxsize=20000)
            threading.Thread(target=self._fm_writer, args=(q,), daemon=True, name='final-minute-writer').start()
        try:
            q.put_nowait(row)
        except queue.Full:
            self.fm_dropped = self.__dict__.get('fm_dropped', 0) + 1     # never block trading for monitoring

    def _fm_writer(self, q):
        while True:
            rows = [q.get()]
            while not q.empty() and len(rows) < 500:
                rows.append(q.get_nowait())
            try:
                with open(self.fm_path, 'a') as f:
                    f.write(''.join(json.dumps(r) + '\n' for r in rows))
            except OSError:
                pass                      # a monitoring file must never interfere with trading
            for _ in rows:
                q.task_done()

    def fm_flush(self):
        """Wait until every queued row is written (tests and shutdown)."""
        q = self.__dict__.get('fm_queue')
        if q is not None:
            q.join()

    def final_minute(self, asset, m, now):
        if not self.s['config'].get('final_minute_log'):
            return
        pending = self.__dict__.setdefault('fm_pending', {})
        for tk, info in list(pending.items()):                    # outcomes of markets that have closed
            if now < info['close'] + OUTCOME_DELAY:
                continue
            avg, n = final_average(self.index_fn(info['asset']), info['close'])
            if n >= OUTCOME_VALUES or now > info['close'] + OUTCOME_GIVE_UP:
                ok = n >= OUTCOME_VALUES            # incomplete: recorded as provisional, never as a result
                self._fm_write(dict(kind='outcome', ticker=tk, asset=info['asset'], close=info['close'], strike=info['strike'],
                                    final_avg=avg if ok else None, values=n, result=(('yes' if avg >= info['strike'] else 'no') if ok else None),
                                    provisional_avg=None if ok else avg))
                del pending[tk]
        close, strike = m.get('close'), m.get('strike')
        if m.get('status') != 'active' or close is None or strike is None or not close - WINDOW <= now < close:
            return
        fp = final_prob(self.index_fn(asset), strike, close, now)
        pending.setdefault(m['ticker'], dict(asset=asset, close=close, strike=strike))
        self._fm_write(dict(kind='snap', t=round(now, 3), asset=asset, ticker=m['ticker'], close=close, strike=strike,
                            sec_left=round(close - now, 3), fetched=m.get('fetched'), model=fp,
                            yes_ask=m.get('yes_ask'), yes_ask_size=m.get('yes_ask_size'), no_ask=m.get('no_ask'), no_ask_size=m.get('no_ask_size'),
                            yes_bid=m.get('yes_bid'), no_bid=m.get('no_bid')))


def fm_path_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), 'final_minute.jsonl')
