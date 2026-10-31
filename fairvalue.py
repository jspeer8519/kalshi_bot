"""Fair probability that a Kalshi 15-minute crypto Up/Down market settles YES.

Contract rules (verified 2026-10-31 from Kalshi's public API, GET /markets/{ticker} ->
``rules_primary`` / ``rules_secondary`` for KXBTC15M, KXETH15M, KXSOL15M, KXDOGE15M; series
settlement source: CF Benchmarks, contract terms assets.kalshi.com/regulatory/product-certifications/CRYPTO.pdf):

* YES if the simple average of the 60 one-second CF Benchmarks Real Time Index values
  (BRTI, ETHUSDRTI, SOLUSDRTI, DOGEUSDRTI) before close is AT LEAST the same 60-second average
  before open. Ties resolve YES.
* The open average is the market's target, published as ``floor_strike``.

The app has no CF Benchmarks feed. It uses 1-second Coinbase spot ticks as a proxy, corrected by
the Coinbase/index gap measured over the minute before open (Coinbase average in that minute
minus ``floor_strike``), the same correction the dashboard chart uses. If the app did not record
that minute the model runs unadjusted and says so (``adjusted`` False).

Model: zero-drift lognormal price with variance per second estimated from the same ticks over
``lookback`` seconds. The arithmetic average of Brownian motion over a window of W seconds has
variance W/3 per unit variance, so before the settlement window the log-variance of the final
average is var * (seconds until the window + 60/3). Inside the window the seconds already
observed are fixed and only the remaining r seconds are random (variance var * r/3).
Only ticks at or before ``now`` are used.
"""
import math
from itertools import pairwise

WINDOW = 60          # seconds averaged at open and at close
MIN_RETURNS = 60     # need at least a minute of 1-second returns to estimate volatility
MAX_GAP = 10         # ignore returns across feed gaps longer than this
MAX_SPOT_AGE = 5     # the latest tick must be this fresh
MIN_BASIS_TICKS = 45


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def variance_per_second(ticks, now, lookback):
    """Realized variance of log price per second over (now-lookback, now], or None."""
    pts = [(t, p) for t, p in ticks if now - lookback <= t <= now and p > 0]
    total = span = 0.0
    n = 0
    for (t0, p0), (t1, p1) in pairwise(pts):
        dt = t1 - t0
        if 0 < dt <= MAX_GAP:
            total += math.log(p1 / p0) ** 2; span += dt; n += 1
    if n < MIN_RETURNS or span <= 0:
        return None
    return total / span


def basis(ticks, open_ts, strike):
    """Coinbase average minus the index average over the minute before open, or None."""
    w = [p for t, p in ticks if open_ts - WINDOW <= t < open_ts]
    if len(w) < MIN_BASIS_TICKS:
        return None
    return sum(w) / len(w) - strike


def _p_above(spot, threshold, var):
    if threshold <= 0:
        return 1.0
    if var <= 0:
        return 1.0 if spot >= threshold else 0.0
    sd = math.sqrt(var)
    return _phi((math.log(spot / threshold) - var / 2) / sd)


def prob_yes(ticks, strike, close, now, lookback=900):
    """Fair P(YES) with its inputs, or None when the inputs are insufficient (fail closed)."""
    if strike is None or strike <= 0 or close is None:
        return None
    ticks = [(t, p) for t, p in ticks if t <= now]
    if not ticks or now - ticks[-1][0] > MAX_SPOT_AGE:
        return None
    var = variance_per_second(ticks, now, lookback)
    if var is None:
        return None
    b = basis(ticks, close - 900, strike)
    adj = b or 0.0
    spot = ticks[-1][1] - adj
    if spot <= 0:
        return None
    start = close - WINDOW
    if now < start:
        p = _p_above(spot, strike, var * ((start - now) + WINDOW / 3))
        observed = None
    else:
        obs = [px - adj for t, px in ticks if start <= t <= now]
        observed = sum(obs) / len(obs) if obs else spot
        elapsed, left = min(now - start, WINDOW), max(close - now, 0)
        if left <= 0:
            p = 1.0 if observed >= strike else 0.0
        else:
            need = (WINDOW * strike - elapsed * observed) / left   # average the rest must reach
            p = _p_above(spot, need, var * left / 3)
    return dict(prob=round(p, 6), spot=ticks[-1][1], index_proxy=round(spot, 8), basis=b, adjusted=b is not None,
                sigma_per_s=math.sqrt(var), seconds_left=close - now, observed_avg=observed)
