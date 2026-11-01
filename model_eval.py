"""Read-only research baseline on the bot's own recorded trades (paper, shadow or live ledger).

Answers, with uncertainty, the questions every strategy change must be judged on:
  1. Net performance: win rate, net P&L, profit factor, expectancy, per-contract P&L, fee drag,
     max drawdown, with bootstrap 95% confidence intervals.
  2. Market calibration: is the price paid (the market's implied probability for the side bought)
     too low or too high compared with how often that side actually won? Brier score, log loss,
     expected calibration error, and per-band edge with Wilson intervals. A positive edge on
     favourites is the hypothesis this bot trades on; this measures it.
  3. Walk-forward probability model: a small L2 logistic regression on information available at
     entry (log-odds of the price paid, minutes left, side, coin). Trained only on earlier trades,
     scored on the next chronological block. Compared with the market's own price as a forecaster.
     Also shows how the out-of-sample trades with a positive model edge after fees performed.
  4. Loss attribution: where the losing dollars came from.

Conventions
- Only trades with a known settlement outcome are used for calibration and the model (settled
  trades, or early exits with settlement tracking). Net-performance figures use every closed trade.
- Profit factor = gross profit of winning trades / |gross loss of losing trades|, after all fees;
  zero-P&L trades count in neither; open positions are excluded.
- Pure standard library: runs in the bot's own virtualenv. Opens the database read-only.

Usage: .venv/bin/python model_eval.py [--mode shadow|paper|live] [--since YYYY-MM-DD] [--out FILE]"""
import argparse
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pnlreport as r  # noqa: E402

SEED = 7
BOOT = 2000
EPS = 1e-6


# ---- statistics ------------------------------------------------------------------------------
def wilson(k, n, z=1.96):
    if n == 0:
        return (float('nan'), float('nan'))
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def profit_factor(pnls):
    gp = sum(x for x in pnls if x > 1e-9); gl = -sum(x for x in pnls if x < -1e-9)
    return (gp / gl) if gl > 0 else None


def bootstrap(trades, stat, n=BOOT, seed=SEED):
    """95% percentile interval of ``stat`` over resampled trade lists (None results dropped)."""
    rnd = random.Random(seed); vals = []
    for _ in range(n):
        v = stat([trades[rnd.randrange(len(trades))] for _ in trades])
        if v is not None:
            vals.append(v)
    if len(vals) < n * .5:
        return None
    vals.sort()
    return (vals[int(.025 * len(vals))], vals[int(.975 * len(vals)) - 1])


def per_contract(ts):
    c = sum(t['size'] for t in ts)
    return sum(t['pnl'] for t in ts) / c if c else None


def max_drawdown(pnls):
    peak = cur = dd = 0.0
    for x in pnls:
        cur += x; peak = max(peak, cur); dd = max(dd, peak - cur)
    return dd


# ---- data ------------------------------------------------------------------------------------
def outcome(t):
    """1 if the side bought settled as the winner, 0 if not, None if unknown."""
    if t['reasons'] == ['settlement']:
        return 1 if t['exit'] >= .5 else 0
    res = next((p.get('settled_result') for p in t['raw'] if p.get('settled_result')), None)
    return None if res is None else int(res == t['side'])


def minutes_left(t):
    close = r.market_close(t['ticker'])
    return None if close is None else max(0.0, (close - t['opened']) / 60)


def load(db, mode, since):
    s = r.load_state(db)
    trades = r.group_trades([t for t in s['trades'] if t.get('mode', 'paper') == mode])
    return sorted((t for t in trades if t['opened'] >= since), key=lambda t: t['opened'])


# ---- logistic regression (L2, Newton-free gradient descent; small data) ---------------------
def features(t, coins):
    p = min(max(t['entry'], EPS), 1 - EPS)
    ml = minutes_left(t) or 0.0
    return [1.0, math.log(p / (1 - p)), ml / 10, 1.0 if t['side'] == 'yes' else 0.0] + [1.0 if t.get('asset') == c else 0.0 for c in coins]


def fit(X, y, l2=1.0, iters=3000, lr=.1):
    w = [0.0] * len(X[0]); n = len(X)
    for _ in range(iters):
        g = [0.0] * len(w)
        for x, yy in zip(X, y, strict=True):
            z = sum(a * b for a, b in zip(w, x, strict=True)); pr = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for j, xj in enumerate(x):
                g[j] += (pr - yy) * xj
        for j in range(len(w)):
            g[j] = g[j] / n + (l2 * w[j] / n if j else 0.0)
            w[j] -= lr * g[j]
    return w


def predict(w, x):
    z = sum(a * b for a, b in zip(w, x, strict=True))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def walk_forward(rows, coins, folds=5, min_train=100):
    """Out-of-fold probabilities: block k is scored by a model trained on blocks 0..k-1 only."""
    n = len(rows); size = n // folds; preds = [None] * n
    for k in range(1, folds):
        lo, hi = k * size, (n if k == folds - 1 else (k + 1) * size)
        if lo < min_train:
            continue
        w = fit([features(t, coins) for t, _ in rows[:lo]], [y for _, y in rows[:lo]])
        for i in range(lo, hi):
            preds[i] = predict(w, features(rows[i][0], coins))
    return preds


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ys)


def logloss(ps, ys):
    return -sum(y * math.log(max(p, EPS)) + (1 - y) * math.log(max(1 - p, EPS)) for p, y in zip(ps, ys, strict=True)) / len(ys)


def ece(ps, ys, bins=10):
    b = defaultdict(list)
    for p, y in zip(ps, ys, strict=True):
        b[min(int(p * bins), bins - 1)].append((p, y))
    return sum(len(v) / len(ps) * abs(sum(p for p, _ in v) / len(v) - sum(y for _, y in v) / len(v)) for v in b.values())


# ---- report ----------------------------------------------------------------------------------
def report(trades, mode, label):
    L = [f'# Model evaluation: {mode.upper()} ledger', '', f'{label}. Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. '
         'Simulated ledgers (paper/shadow) are not evidence of live profitability.', '']
    if not trades:
        return '\n'.join(L + ['No closed trades in this period.'])
    pn = [t['pnl'] for t in trades]; wins = [x for x in pn if x > 1e-9]; losses = [x for x in pn if x < -1e-9]
    fees = sum(sum(p.get('entry_fee', 0) + p.get('exit_fee', 0) for p in t['raw']) for t in trades)
    pf = profit_factor(pn); pc = per_contract(trades)
    pc_ci = bootstrap(trades, per_contract); pf_ci = bootstrap(trades, lambda ts: profit_factor([t['pnl'] for t in ts]))
    lo, hi = wilson(len(wins), len(trades))
    L += ['## 1. Net performance', '',
          '| Metric | Value | 95% interval |', '|---|---|---|',
          f'| Closed trades | {len(trades)} | |',
          f'| Win rate | {len(wins) / len(trades):.1%} | {lo:.1%} to {hi:.1%} (Wilson) |',
          f'| Net P&L | ${sum(pn):+,.2f} | |',
          f'| Profit factor | {pf:.2f} | ' + (f'{pf_ci[0]:.2f} to {pf_ci[1]:.2f} (bootstrap)' if pf and pf_ci else 'n/a') + ' |' if pf else '| Profit factor | n/a (no losing trades) | |',
          f'| Expectancy per trade | ${sum(pn) / len(pn):+.2f} | |',
          f'| Per contract | {pc * 100:+.2f}c | ' + (f'{pc_ci[0] * 100:+.2f}c to {pc_ci[1] * 100:+.2f}c (bootstrap)' if pc_ci else 'n/a') + ' |',
          f'| Average win / loss | ${sum(wins) / max(1, len(wins)):.2f} / ${sum(losses) / max(1, len(losses)):.2f} | |',
          f'| Fees paid | ${fees:,.2f} ({fees / sum(wins) * 100 if wins else 0:.1f}% of gross profit) | |',
          f'| Max drawdown (realized, trade order) | ${max_drawdown(pn):,.2f} | |', '']
    if pc_ci:
        L.append('Edge verdict: ' + ('**positive with 95% confidence** (interval above 0).' if pc_ci[0] > 0 else
                                     '**not established**: the interval includes 0 or is negative. More trades needed before scaling.') )
        L.append('')

    known = [(t, outcome(t)) for t in trades]; known = [(t, y) for t, y in known if y is not None]
    L += ['## 2. Market calibration (price paid vs. how often that side won)', '', f'{len(known)} trades with a known settlement outcome.', '']
    if known:
        ps = [min(max(t['entry'], EPS), 1 - EPS) for t, _ in known]; ys = [y for _, y in known]
        L += [f'Brier {brier(ps, ys):.4f} · log loss {logloss(ps, ys):.4f} · ECE {ece(ps, ys):.4f} (market price as the forecast)', '',
              '| Price paid | Trades | Avg price | Actual win | 95% interval | Edge (actual - price) |', '|---|---|---|---|---|---|']
        for a, b in ((0, .70), (.70, .80), (.80, .84), (.84, .88), (.88, .92), (.92, 1.01)):
            g = [(p, y) for p, y in zip(ps, ys, strict=True) if a <= p < b]
            if g:
                k = sum(y for _, y in g); w_lo, w_hi = wilson(k, len(g)); ap = sum(p for p, _ in g) / len(g)
                L.append(f'| {a * 100:.0f}-{min(b, 1) * 100:.0f}c | {len(g)} | {ap * 100:.1f}c | {k / len(g):.1%} | {w_lo:.1%} to {w_hi:.1%} | {(k / len(g) - ap) * 100:+.1f} pts |')
        L += ['', 'An edge whose interval sits wholly above the price means that band was underpriced (the strategy\'s hypothesis).', '']

    L += ['## 3. Walk-forward probability model', '']
    if len(known) < 250:
        L += [f'Skipped: {len(known)} trades with known outcomes; at least 250 are needed for 5 chronological folds with 100+ training trades.', '']
    else:
        coins = sorted({t.get('asset') for t, _ in known if t.get('asset')})[1:]          # first coin is the baseline
        preds = walk_forward(known, coins)
        idx = [i for i, p in enumerate(preds) if p is not None]
        mp = [min(max(known[i][0]['entry'], EPS), 1 - EPS) for i in idx]; md = [preds[i] for i in idx]; yy = [known[i][1] for i in idx]
        L += [f'Out-of-sample trades: {len(idx)} (each scored by a model trained only on earlier trades).', '',
              '| Forecaster | Brier | Log loss | ECE |', '|---|---|---|---|',
              f'| Market price | {brier(mp, yy):.4f} | {logloss(mp, yy):.4f} | {ece(mp, yy):.4f} |',
              f'| Logistic model | {brier(md, yy):.4f} | {logloss(md, yy):.4f} | {ece(md, yy):.4f} |', '',
              ('The model **beats** the market price out of sample (lower Brier).' if brier(md, yy) < brier(mp, yy) else
               'The model does **not** beat the market price out of sample; do not trade on it.'), '']
        for margin in (0.0, .02, .04):
            sel = [known[i][0] for i in idx if preds[i] - known[i][0]['entry'] - .01 >= margin]
            if sel:
                L.append(f'- Out-of-sample trades where model edge after ~1c fee >= {margin * 100:.0f}c: {len(sel)} of {len(idx)}, '
                         f'per contract {per_contract(sel) * 100:+.2f}c (all out-of-sample: {per_contract([known[i][0] for i in idx]) * 100:+.2f}c)')
        L.append('')

    L += ['## 4. Loss attribution (share of losing dollars)', '']
    total = -sum(losses) or 1
    for title, key in (('Entry price', lambda t: f'{int(t["entry"] * 100) // 4 * 4}-{int(t["entry"] * 100) // 4 * 4 + 3}c'),
                       ('Minutes left at entry', lambda t: 'unknown' if minutes_left(t) is None else f'{int(minutes_left(t))}-{int(minutes_left(t)) + 1} min'),
                       ('Coin', lambda t: t.get('asset') or '?'), ('Side', lambda t: t['side'].upper()), ('Exit', lambda t: '+'.join(t['reasons'])),
                       ('Hour (UTC)', lambda t: f'{datetime.fromtimestamp(t["opened"], timezone.utc).hour:02d}:00')):
        g = defaultdict(lambda: [0, 0.0, 0])
        for t in trades:
            k = key(t); g[k][2] += 1
            if t['pnl'] < -1e-9:
                g[k][0] += 1; g[k][1] += -t['pnl']
        rows = sorted(g.items(), key=lambda kv: -kv[1][1])[:6]
        L.append(f'**{title}:** ' + '; '.join(f'{k} {v[1] / total:.0%} of losses ({v[0]} of {v[2]} trades lost)' for k, v in rows if v[0]))
    L += ['', 'Reading note (always shown): in the breakdowns above, a group with fewer than ~30 trades is too small to act on. This is not an error.']
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'), default='shadow')
    ap.add_argument('--since', default='')
    ap.add_argument('--db', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'paper.sqlite3'))
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    since = datetime.strptime(a.since, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() if a.since else 0
    if not os.path.exists(a.db):
        sys.exit(f'Database not found: {a.db} (run this from the bot folder, e.g. cd ~/kalshi)')
    trades = load(a.db, a.mode, since)
    print(f'Loaded {len(trades)} closed {a.mode} trades from {a.db}' + (f' since {a.since}' if a.since else '') +
          ('; fitting the walk-forward model, this can take about a minute...' if len(trades) >= 250 else '...'), file=sys.stderr, flush=True)
    text = report(trades, a.mode, f'Trades opened since {a.since}' if a.since else 'All recorded trades')
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, 'w') as f:
            f.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
