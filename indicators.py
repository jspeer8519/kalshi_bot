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


def summarize(candles, keep=60):
    """Indicators computed over all candles; the last ``keep`` points are returned."""
    closes = [c['c'] for c in candles]
    fast, slow, strength, rng = ema(closes, 9), ema(closes, 21), rsi(closes, 14), atr(candles, 14)
    tail = slice(-keep, None)
    return dict(ema9=fast[tail], ema21=slow[tail], rsi14=strength[tail], atr14=rng[-1] if rng else None)
