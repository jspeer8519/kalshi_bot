"""Technical indicators on 1-minute candles (dicts with t, o, h, l, c), oldest first.

Standard textbook definitions, no tuning:
* EMA(n): exponential moving average of closes, alpha = 2/(n+1), seeded with the SMA of the first n closes.
* RSI(n): Wilder's relative strength index of closes (Wilder smoothing, alpha = 1/n).
* ATR(n): Wilder's average true range.

Series are aligned with the input candles; values before the warm-up are None.
"""


def ema(closes, n):
    out = [None] * len(closes)
    if len(closes) < n:
        return out
    value = sum(closes[:n]) / n
    out[n - 1] = value
    k = 2 / (n + 1)
    for i in range(n, len(closes)):
        value = closes[i] * k + value * (1 - k)
        out[i] = value
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    avg_gain, avg_loss = gains / n, losses / n

    def value():
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    out[n] = value()
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n
        out[i] = value()
    return out


def atr(candles, n=14):
    out = [None] * len(candles)
    if len(candles) <= n:
        return out
    trs = [None]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]['h'], candles[i]['l'], candles[i - 1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    value = sum(trs[1:n + 1]) / n
    out[n] = value
    for i in range(n + 1, len(candles)):
        value = (value * (n - 1) + trs[i]) / n
        out[i] = value
    return out


def bollinger(closes, n=20, k=2.0):
    """Bollinger Bands: (middle, upper, lower), SMA(n) ± k × the population standard deviation of
    the same n closes (Bollinger's definition). Aligned with ``closes``; None before the warm-up."""
    mid, up, lo = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** .5
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


def percent_b(price, upper, lower):
    """%B: 0 at the lower band, 1 at the upper band, below 0 / above 1 outside. 0.5 if the bands are flat."""
    width = upper - lower
    return .5 if width <= 0 else (price - lower) / width


MAX_TICK_AGE = 5     # seconds: the latest price must be this fresh (same rule as the fair-value model)


def minute_closes(ticks, now):
    """1-minute closes from 1-second ticks [(unix second, price)], oldest first: the last price of
    every complete minute, then the current price as the close of the forming minute (as live
    charts do). Uses only ticks at or before ``now``. Returns the longest run of consecutive
    minutes ending now; a minute with no tick breaks the run, so a feed gap is never bridged."""
    pts = [(t, p) for t, p in ticks if t <= now and p > 0]
    if not pts or now - pts[-1][0] > MAX_TICK_AGE:
        return []
    last = {}
    for t, p in pts:
        last[int(t // 60)] = p
    cur = int(now // 60)
    if cur not in last:                      # no tick yet in this minute: the latest price is its close so far
        last[cur] = pts[-1][1]
    out, m = [], cur
    while m in last:
        out.append(last[m]); m -= 1
    return out[::-1]


def bollinger_now(ticks, now, n=20, k=2.0):
    """Current Bollinger state on 1-minute closes of ``ticks``, or None without n consecutive minutes
    of fresh data: dict(price, mid, upper, lower, pctb, width)."""
    closes = minute_closes(ticks, now)
    if len(closes) < n:
        return None
    mid, up, lo = (s[-1] for s in bollinger(closes[-n:], n, k))
    price = closes[-1]
    return dict(price=price, mid=round(mid, 8), upper=round(up, 8), lower=round(lo, 8),
                pctb=round(percent_b(price, up, lo), 4), width=round(up - lo, 8))


def summarize(candles, keep=60):
    """Indicators computed over all candles; the last ``keep`` points are returned."""
    closes = [c['c'] for c in candles]
    fast, slow, strength, rng = ema(closes, 9), ema(closes, 21), rsi(closes, 14), atr(candles, 14)
    bb_mid, bb_up, bb_lo = bollinger(closes, 20, 2.0)
    tail = slice(-keep, None)
    pctb = percent_b(closes[-1], bb_up[-1], bb_lo[-1]) if closes and bb_up[-1] is not None else None
    return dict(ema9=fast[tail], ema21=slow[tail], rsi14=strength[tail], atr14=rng[-1] if rng else None,
                bb_mid=bb_mid[tail], bb_up=bb_up[tail], bb_lo=bb_lo[tail], bb_pctb=pctb)


REVERSAL_FRESH = 2.5   # seconds: the latest index value must be at least this recent
REVERSAL_MAX_STEP = 1.5  # seconds: largest allowed gap between consecutive index values in the window


def reversal(ticks, strike, side, now, pct=0.02, seconds=5):
    """Has the price crossed the market target AGAINST ``side`` and stayed there? YES needs the final
    average at or above ``strike``, so a reversal against YES is the price at least ``pct`` percent BELOW
    it; against NO, at least ``pct`` percent ABOVE it. Uses only values at or before ``now``. The
    confirmation window [now - seconds, now] must be covered by consecutive 1-second index values
    (no gap over REVERSAL_MAX_STEP, first value at the window start, latest no older than
    REVERSAL_FRESH) and every one of them must be past the line. Missing data never confirms.
    Returns dict(price, strike, line, held) or None."""
    if not strike or strike <= 0 or side not in ('yes', 'no'):
        return None
    pts = sorted((t, p) for t, p in ticks if t <= now and p > 0)
    if not pts or now - pts[-1][0] > REVERSAL_FRESH:
        return None
    line = strike * (1 - pct / 100) if side == 'yes' else strike * (1 + pct / 100)
    past = (lambda p: p < line) if side == 'yes' else (lambda p: p > line)
    window = [(t, p) for t, p in pts if t >= now - seconds] if seconds > 0 else pts[-1:]
    if seconds > 0:
        if not window or window[0][0] > now - seconds + REVERSAL_MAX_STEP:
            return None                                          # the start of the window is not covered
        if any(b[0] - a[0] > REVERSAL_MAX_STEP for a, b in zip(window, window[1:])):
            return None                                          # a gap: missing values cannot confirm anything
    if not all(past(p) for _, p in window):
        return None
    return dict(price=pts[-1][1], strike=strike, line=round(line, 8), held=seconds)
