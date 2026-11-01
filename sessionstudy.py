"""Read-only study: what did markets look like at 10 minutes left, and which early signs separated
sessions where every coin won from sessions with 1, 2 or 3+ losing coins?

For each 15-minute session the bot traded, it downloads Kalshi's public 1-minute history (YES bid/ask,
last price, volume) for every coin's market in that session (no credentials; cached in
output/cache/candles so reruns are instant), measures each market at 10 minutes left (the candle
ending 600 s before close), and compares those measurements with how the bot's trades settled.

Measurements at 10 minutes left (from the bot's side of each trade):
  fav_strength   distance of the YES midpoint from 50c (how lopsided the market was)
  agreed_at_10   1 if the side the bot later bought was already the favourite
  drift_0_10     change of the bot side's midpoint from the first minute to 10 min left (+ = toward it)
  path_0_10      total minute-to-minute movement of the midpoint in the first 5 minutes (choppiness)
  flips_0_10     number of times the favourite changed side in the first 5 minutes
  volume_0_10    contracts traded in the first 5 minutes
  coins_agree    share of the other coins whose favourite at 10 min left was the bot's side
  peak_before    highest price of the bot's side before the entry minute ("fell into range" if above 92c)

Every split is reported for the first and second half of the sessions separately: a pattern that
holds in only one half is probably chance. Many measurements are compared, so expect some to look
different by luck; act only on patterns that hold in both halves with enough trades.

Usage: .venv/bin/python sessionstudy.py [--mode shadow|paper] [--since YYYY-MM-DD] [--offline]"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from itertools import pairwise

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import pnlreport as r  # noqa: E402
from settings import ALL_ASSETS  # noqa: E402

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
AT = 600            # "10 minutes left"
ENTRY_MAX = .92


def num(x):
    try:
        v = float(x); return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def candles(ticker, close, cache, offline):
    """[(end_ts, yes_bid, yes_ask, volume)] for the market's 15 minutes, oldest first ([] if unavailable)."""
    path = os.path.join(cache, ticker + '.json')
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
    elif offline:
        return []
    else:
        series = ticker.split('-')[0]
        url = f'{KALSHI}/series/{series}/markets/{ticker}/candlesticks?start_ts={int(close) - 960}&end_ts={int(close)}&period_interval=1'
        try:
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'KalshiDesk-study/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.load(resp)
        except Exception:
            return []
        time.sleep(.12)                                    # stay well under Kalshi's public rate limits
        os.makedirs(cache, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(raw, f)
    out = []
    for c in raw.get('candlesticks') or []:
        b, a = num((c.get('yes_bid') or {}).get('close_dollars')), num((c.get('yes_ask') or {}).get('close_dollars'))
        if b is not None and a is not None and a > 0:
            out.append((int(c['end_period_ts']), b, a, num(c.get('volume_fp')) or 0.0))
    return sorted(out)


def mid(c):
    return (c[1] + c[2]) / 2


def side_price(c, side):
    """Cost to buy ``side`` at the close of candle ``c``."""
    return c[2] if side == 'yes' else 1 - c[1]


def measure(cs, close, side):
    """Measurements at 10 minutes left from the bot's side, or None without the candles."""
    early = [c for c in cs if c[0] <= close - AT]
    if len(early) < 3:
        return None
    m = [mid(c) if side == 'yes' else 1 - mid(c) for c in early]           # the bot side's midpoint
    flips = sum(1 for a, b in pairwise(m) if (a - .5) * (b - .5) < 0)
    return dict(fav_strength=abs(m[-1] - .5), agreed_at_10=1.0 if m[-1] > .5 else 0.0, drift_0_10=m[-1] - m[0],
                path_0_10=sum(abs(b - a) for a, b in pairwise(m)), flips_0_10=float(flips),
                volume_0_10=sum(c[3] for c in early))


def favourite(cs, close):
    early = [c for c in cs if c[0] <= close - AT]
    if not early:
        return None
    x = mid(early[-1]) - .5
    return None if abs(x) < 1e-9 else ('yes' if x > 0 else 'no')


def study(trades, cache, offline, log=print):
    sessions = defaultdict(list)
    for t in trades:
        if t['reasons'] != ['settlement']:
            continue
        sessions[t['ticker'].split('-')[1]].append(t)
    rows = []; n = 0
    for code, ts in sorted(sessions.items(), key=lambda kv: min(t['opened'] for t in kv[1])):
        close = ts[0]['raw'][0].get('close') or r.market_close(ts[0]['ticker'])
        suffix = ts[0]['ticker'].split('-')[2]
        books = {}
        for coin in ALL_ASSETS:
            tk = f'KX{coin}15M-{code}-{suffix}'
            books[coin] = candles(tk, close, cache, offline)
            n += 1
            if n % 60 == 0:
                log(f'  ... {n} markets loaded', file=sys.stderr)
        fav = {coin: favourite(cs, close) for coin, cs in books.items()}
        losers = sum(1 for t in ts if t['pnl'] < 0)
        for t in ts:
            coin = t.get('asset') or t['ticker'][2:].split('15M')[0]
            ms = measure(books.get(coin) or [], close, t['side'])
            if ms is None:
                continue
            others = [f for c, f in fav.items() if c != coin and f]
            ms['coins_agree'] = sum(f == t['side'] for f in others) / len(others) if others else None
            before = [side_price(c, t['side']) for c in books[coin] if c[0] <= t['opened'] - 60]
            ms['peak_before'] = max(before) if before else None
            rows.append(dict(t=t, code=code, losers=losers, won=t['pnl'] > 0, **ms))
    return rows, len(sessions)


def terciles(rows, key):
    vals = sorted(x[key] for x in rows if x.get(key) is not None)
    if len(vals) < 9:
        return None
    return vals[len(vals) // 3], vals[2 * len(vals) // 3]


def loss_rate(g):
    return (sum(not x['won'] for x in g) / len(g)) if g else None


def report(rows, n_sessions, mode, label):
    L = [f'# Session study: {mode.upper()} ledger, measured at 10 minutes left', '', f'{label}. {n_sessions} sessions, {len(rows)} settled trades with Kalshi history.',
         'Simulated ledger; patterns need both halves to agree before acting.', '']
    if not rows:
        return '\n'.join(L + ['No trades with history (offline without a cache, or no settled trades).'])
    by_sess = defaultdict(list)
    for x in rows:
        by_sess[x['code']].append(x)
    L += ['## 1. Sessions by number of losing coins (averages over the coins traded in the session)', '',
          '| Losing coins | Sessions | Fav strength | Already favourite | Drift 0-10 | Choppiness | Flips | Other coins agree | Volume 0-10 |', '|---|---|---|---|---|---|---|---|---|']
    groups = defaultdict(list)
    for xs in by_sess.values():
        groups[min(xs[0]['losers'], 3)].append(xs)
    avg = lambda xs, k: (lambda v: sum(v) / len(v) if v else float('nan'))([x[k] for x in xs if x.get(k) is not None])
    for k in (0, 1, 2, 3):
        ss = groups.get(k)
        if not ss:
            continue
        flat = [x for xs in ss for x in xs]
        L.append(f'| {k if k < 3 else "3+"} | {len(ss)} | {avg(flat, "fav_strength") * 100:.1f}c | {avg(flat, "agreed_at_10"):.0%} | {avg(flat, "drift_0_10") * 100:+.1f}c | '
                 f'{avg(flat, "path_0_10") * 100:.1f}c | {avg(flat, "flips_0_10"):.2f} | {avg(flat, "coins_agree"):.0%} | {avg(flat, "volume_0_10"):,.0f} |')
    half = sorted({x['code'] for x in rows}, key=lambda c: min(x['t']['opened'] for x in by_sess[c]))
    first = set(half[:len(half) // 2])
    L += ['', '## 2. Which early measurement separates winning from losing trades?', '',
          'Loss rate of trades split into thirds by each measurement (low / middle / high third), for all trades and for each half of the sessions.', '',
          '| Measurement | Low third | Middle | High third | Gap (high - low) | 1st half gap | 2nd half gap | Both halves agree |', '|---|---|---|---|---|---|---|---|']
    ranked = []
    for key, name in (('fav_strength', 'Favourite strength'), ('agreed_at_10', 'Already favourite at 10 min'), ('drift_0_10', 'Drift toward bot side'),
                      ('path_0_10', 'Choppiness (first 5 min)'), ('flips_0_10', 'Favourite flips'), ('volume_0_10', 'Volume (first 5 min)'),
                      ('coins_agree', 'Other coins agree'), ('peak_before', 'Peak price before entry')):
        if key in ('agreed_at_10', 'flips_0_10'):
            cut = (0.5, 0.5)                                 # 0/1 style: low = 0, high = 1+
        else:
            cut = terciles(rows, key)
        if not cut:
            continue
        def split(g, cut=cut, key=key):
            lo = [x for x in g if x.get(key) is not None and x[key] < cut[0]]
            hi = [x for x in g if x.get(key) is not None and x[key] >= cut[1]]
            mi = [x for x in g if x.get(key) is not None and cut[0] <= x[key] < cut[1]]
            return lo, mi, hi
        lo, mi, hi = split(rows)
        g1 = split([x for x in rows if x['code'] in first]); g2 = split([x for x in rows if x['code'] not in first])
        gap = lambda s: (loss_rate(s[2]) - loss_rate(s[0])) if s[0] and s[2] else None
        a, b, c = gap((lo, mi, hi)), gap(g1), gap(g2)
        fmt = lambda g: f'{loss_rate(g):.0%} ({len(g)})' if g else '-'
        agree = 'yes' if (b is not None and c is not None and b * c > 0 and abs(b) >= .03 and abs(c) >= .03) else 'no'
        ranked.append((abs(a or 0) if agree == 'yes' else -1, name, a))
        L.append(f'| {name} | {fmt(lo)} | {fmt(mi)} | {fmt(hi)} | {a * 100:+.0f} pts |' if a is not None else f'| {name} | {fmt(lo)} | {fmt(mi)} | {fmt(hi)} | - |')
        L[-1] += f' {b * 100:+.0f} pts | {c * 100:+.0f} pts | {agree} |' if b is not None and c is not None else ' - | - | no |'
    fell = [x for x in rows if x.get('peak_before') is not None and x['peak_before'] > ENTRY_MAX]
    stay = [x for x in rows if x.get('peak_before') is not None and x['peak_before'] <= ENTRY_MAX]
    L += ['', f'**Fell into range** (side was above {ENTRY_MAX * 100:.0f}c before entry): ' +
          (f'{loss_rate(fell):.0%} lost ({len(fell)} trades) vs {loss_rate(stay):.0%} ({len(stay)}) for the rest.' if fell and stay else 'not enough trades.')]
    best = [x for x in sorted(ranked, reverse=True) if x[0] > 0][:3]
    L += ['', '## 3. Strongest early signs (consistent in both halves)', '']
    L += [f'- {name}: losses {"rise" if a > 0 else "fall"} by {abs(a) * 100:.0f} points from the low to the high third.' for _, name, a in best] or \
         ['- None yet: no measurement separated losers from winners consistently in both halves. More sessions needed.']
    L += ['', 'Next step for any sign listed here: build it as an off-by-default filter and test it on new sessions (settings label), never on the sessions it was found in.']
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'), default='shadow')
    ap.add_argument('--since', default='')
    ap.add_argument('--db', default=os.path.join(ROOT, 'data', 'paper.sqlite3'))
    ap.add_argument('--cache', default=os.path.join(ROOT, 'output', 'cache', 'candles'))
    ap.add_argument('--offline', action='store_true', help='use cached history only')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        sys.exit(f'Database not found: {a.db} (run this from the bot folder, e.g. cd ~/kalshi)')
    since = datetime.strptime(a.since, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() if a.since else 0
    s = r.load_state(a.db)
    trades = [t for t in r.group_trades([t for t in s['trades'] if t.get('mode', 'paper') == a.mode]) if t['opened'] >= since]
    print(f'{len(trades)} closed {a.mode} trades; downloading Kalshi 1-minute history for every coin in those sessions '
          '(first run can take a few minutes; cached afterwards)...', file=sys.stderr, flush=True)
    rows, n = study(trades, a.cache, a.offline, log=lambda *x, **k: print(*x, **k))
    text = report(rows, n, a.mode, f'Trades opened since {a.since}' if a.since else 'All recorded trades')
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, 'w') as f:
            f.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
