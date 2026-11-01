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

from fairvalue import MAX_SPOT_AGE, WINDOW, _phi, variance_per_second

LOOKBACK = 900           # seconds of 1-second returns for the volatility estimate
MIN_OUTCOME_VALUES = 55  # index values needed in the final minute to compute the outcome
OUTCOME_DELAY = 3        # seconds after close before the outcome is computed
OUTCOME_GIVE_UP = 300


def final_prob(ticks, strike, close, now, lookback=LOOKBACK):
    """P(the settlement average is at least ``strike``) inside the final minute, or None.
    dict(p_yes, known, remaining, partial_avg, price)."""
    if strike is None or not close - WINDOW <= now < close:
        return None
    pts = [(t, p) for t, p in ticks if t <= now and p > 0]
    if not pts or now - pts[-1][0] > MAX_SPOT_AGE:
        return None
    known = [p for t, p in pts if close - WINDOW <= t < close]
    price, k = pts[-1][1], len(known)
    r = max(0, WINDOW - k)
    var = variance_per_second(ticks, now, lookback)
    if var is None:
        return None
    need = WINDOW * strike - sum(known) - r * price          # how far the sum must still move (positive = YES behind)
    sd = price * math.sqrt(var) * math.sqrt(r * (r + 1) * (2 * r + 1) / 6) if r else 0.0
    p = (1.0 if need <= 0 else 0.0) if sd <= 0 else 1 - _phi(need / sd)
    return dict(p_yes=round(p, 6), known=k, remaining=r, partial_avg=round(sum(known) / k, 8) if k else None, price=price)


def final_average(ticks, close):
    """Settlement value from recorded index values: mean of those in [close-60, close), with their count."""
    vals = [p for t, p in ticks if close - WINDOW <= t < close and p > 0]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


class FinalMinute:
    """Mixed into Engine. ``self.fm_path`` and ``self.index_fn`` (settlement index only, never the Coinbase
    fallback, so outcomes and probabilities are computed on the values Kalshi settles on) are set by Engine.__init__."""

    def _fm_write(self, row):
        try:
            with open(self.fm_path, 'a') as f:
                f.write(json.dumps(row) + '\n')
        except OSError:
            pass                      # a monitoring file must never interfere with trading

    def final_minute(self, asset, m, now):
        if not self.s['config'].get('final_minute_log'):
            return
        pending = self.__dict__.setdefault('fm_pending', {})
        for tk, info in list(pending.items()):                    # outcomes of markets that have closed
            if now < info['close'] + OUTCOME_DELAY:
                continue
            avg, n = final_average(self.index_fn(info['asset']), info['close'])
            if n >= MIN_OUTCOME_VALUES or now > info['close'] + OUTCOME_GIVE_UP:
                ok = n >= MIN_OUTCOME_VALUES
                self._fm_write(dict(kind='outcome', ticker=tk, asset=info['asset'], close=info['close'], strike=info['strike'],
                                    final_avg=avg if ok else None, values=n, result=(('yes' if avg >= info['strike'] else 'no') if ok else None)))
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
