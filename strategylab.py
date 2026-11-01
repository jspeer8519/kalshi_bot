"""Read-only strategy lab: reconciliation, net P&L breakdowns, break-even by price band, coin ranking,
and an out-of-sample comparison of entry policies, with session-clustered uncertainty.

Why sessions, not trades: coins in one 15-minute session move together, so their outcomes are not
independent. All confidence intervals resample whole sessions (cluster bootstrap).

Sections
 1. Reconciliation: ledger rows -> closed trades -> settled trades -> trades with Kalshi history; duplicate
    check; P&L totals must agree.
 2. Net P&L per coin, per UTC day, worst sessions (recorded entry prices, recorded fees, contracts, settlement).
 3. Break-even by entry-price band: cost per contract incl. fees = the win rate needed; flags bands whose
    win rate looks high but whose expected value is negative or unproven.
 4. Coins ranked by calibrated net EV per contract: win rate shrunk toward the pooled rate (20 pseudo-trades),
    minus cost; session-clustered 95% interval; average contracts filled as a liquidity hint.
 5. Out-of-sample comparison (policy chosen on the first half of the sessions, scored on the second):
    existing strategy, one market per session, single best coin, cross-coin agreement filter, and
    agreement + one per session. Agreement uses only other coins' favourites from complete minutes that
    ended at or before each trade's entry (Kalshi public history, cached; see sessionstudy.py).
 6. Settings versions (prospective test): results per settings label with session-clustered intervals.

Limits: policies can only select among trades the bot actually took; trades it never took are not
simulated. Results are also shown scaled to $10 per trade (each trade's P&L x 10 / its cost).

Usage: .venv/bin/python strategylab.py [--mode shadow] [--since YYYY-MM-DD] [--offline] [--no-history]"""
import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import pnlreport as r  # noqa: E402
import sessionstudy as ss  # noqa: E402
from model_eval import profit_factor, wilson  # noqa: E402

SEED, BOOT, SHRINK = 11, 2000, 20
THRESHOLDS = (.5, .6, .7, .8)
SCALE_TO = 10.0


# ---- helpers ---------------------------------------------------------------------------------
def session_of(t):
    return t['ticker'].split('-')[1] if t['ticker'].count('-') >= 2 else t['ticker']


def fees(t):
    return sum(p.get('entry_fee', 0) + p.get('exit_fee', 0) for p in t['raw'])


def cost_per_contract(t):
    return t['cost'] / t['size'] if t['size'] else None


def scaled(t):
    return t['pnl'] * SCALE_TO / t['cost'] if t['cost'] else 0.0


def per_contract(ts):
    c = sum(t['size'] for t in ts)
    return sum(t['pnl'] for t in ts) / c if c else None


def cluster_ci(ts, stat=per_contract, n=BOOT, seed=SEED):
    """95% interval of ``stat`` resampling whole sessions."""
    by = defaultdict(list)
    for t in ts:
        by[session_of(t)].append(t)
    keys = list(by)
    if len(keys) < 5:
        return None
    rnd = random.Random(seed); vals = []
    for _ in range(n):
        sample = [t for _ in keys for t in by[keys[rnd.randrange(len(keys))]]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    vals.sort()
    return (vals[int(.025 * len(vals))], vals[int(.975 * len(vals)) - 1]) if vals else None


def metrics(ts, value=lambda t: t['pnl']):
    """Net, average win/loss, profit factor, worst session, max drawdown (session order)."""
    if not ts:
        return None
    pn = [value(t) for t in ts]
    wins = [x for x in pn if x > 1e-9]; losses = [x for x in pn if x < -1e-9]
    by = defaultdict(float); order = {}
    for t in ts:
        k = session_of(t); by[k] += value(t); order[k] = min(order.get(k, t['opened']), t['opened'])
    peak = cur = dd = 0.0
    for k in sorted(by, key=order.get):
        cur += by[k]; peak = max(peak, cur); dd = max(dd, peak - cur)
    return dict(n=len(ts), sessions=len(by), net=sum(pn), avg_win=sum(wins) / len(wins) if wins else 0.0,
                avg_loss=sum(losses) / len(losses) if losses else 0.0, pf=profit_factor(pn), worst_session=min(by.values()),
                dd=dd, win=len(wins) / len(ts), pc=per_contract(ts))


def fmt_pf(x):
    return 'n/a' if x is None else f'{x:.2f}'


# ---- sections --------------------------------------------------------------------------------
def reconcile(raw_rows, trades, settled, with_hist, early_skipped):
    keys = [(t.get('mode', 'paper'), t['ticker'], t['opened']) for t in raw_rows]
    dup = len(raw_rows) - len({(t['ticker'], t['opened'], t['closed'], t['size'], round(t['pnl'], 6)) for t in raw_rows})
    sum_raw = sum(t['pnl'] for t in raw_rows); sum_grouped = sum(t['pnl'] for t in trades)
    sessions = {session_of(t) for t in settled}
    return ['## 1. Reconciliation', '',
            '| Step | Count | Note |', '|---|---|---|',
            f'| Ledger rows (closed fills, incl. partial exits) | {len(raw_rows)} | {len(set(keys))} distinct positions (mode, ticker, entry time) |',
            f'| Closed trades (partial exits combined) | {len(trades)} | one per position |',
            f'| Settled at expiry | {len(settled)} | {len(trades) - len(settled)} exited early (no settlement outcome) |',
            f'| Sessions with a settled trade | {len(sessions)} | |',
            f'| Settled trades with Kalshi history | {with_hist} | measured by the study |',
            f'| Excluded: entered before 10 min left | {early_skipped} | their 10-minute features would be future data |',
            f'| Exact duplicate ledger rows | {dup} | must be 0 |',
            f'| P&L: ledger rows vs closed trades | ${sum_raw:+,.2f} vs ${sum_grouped:+,.2f} | {"equal" if abs(sum_raw - sum_grouped) < .005 else "MISMATCH"} |', '',
            'The study\'s first line ("N closed trades") counts every closed trade since the date; its report counts settled trades '
            'that had Kalshi history. The second can never exceed the first.', '']


def breakdowns(trades):
    L = ['## 2. Net P&L', '']
    for title, key in (('Coin', lambda t: t['asset']), ('UTC day', lambda t: datetime.fromtimestamp(t['closed'], timezone.utc).strftime('%Y-%m-%d'))):
        g = defaultdict(list)
        for t in trades:
            g[key(t)].append(t)
        L += [f'**By {title.lower()}**', '', f'| {title} | Trades | Contracts | Won | Net | Fees | Per contract |', '|---|---|---|---|---|---|---|']
        for k in sorted(g):
            ts = g[k]
            L.append(f'| {k} | {len(ts)} | {sum(t["size"] for t in ts):g} | {sum(t["pnl"] > 0 for t in ts) / len(ts):.0%} | ${sum(t["pnl"] for t in ts):+,.2f} | '
                     f'${sum(fees(t) for t in ts):,.2f} | {per_contract(ts) * 100:+.2f}c |')
        L.append('')
    by = defaultdict(list)
    for t in trades:
        by[session_of(t)].append(t)
    worst = sorted(by.items(), key=lambda kv: sum(t['pnl'] for t in kv[1]))[:5]
    L += ['**Worst sessions**', '', '| Session | Coins | Losers | Net |', '|---|---|---|---|']
    L += [f'| {k} | {len(ts)} | {sum(t["pnl"] < 0 for t in ts)} | ${sum(t["pnl"] for t in ts):+,.2f} |' for k, ts in worst]
    return L + ['']


def breakeven(settled):
    L = ['## 3. Break-even by entry price', '', 'Break-even win rate = cost per contract including fees (a winning contract pays $1).', '',
         '| Price band | Trades | Avg cost/contract (= break-even) | Actual win | 95% interval | EV per contract | Session-clustered EV interval | Flag |',
         '|---|---|---|---|---|---|---|---|']
    for lo in range(70, 100, 3):
        g = [t for t in settled if lo / 100 <= t['entry'] < (lo + 3) / 100]
        if not g:
            continue
        be = sum(t['cost'] for t in g) / sum(t['size'] for t in g); k = sum(t['pnl'] > 0 for t in g); w = k / len(g)
        wl, wh = wilson(k, len(g)); ev = per_contract(g); ci = cluster_ci(g)
        flag = ('NEGATIVE EV despite high win rate' if ev < 0 and w >= .85 else 'negative EV' if ev < 0 else
                'unproven (interval includes 0)' if not ci or ci[0] <= 0 else 'positive')
        L.append(f'| {lo}-{lo + 3}c | {len(g)} | {be * 100:.1f}c | {w:.1%} | {wl:.0%}-{wh:.0%} | {ev * 100:+.2f}c | '
                 + (f'{ci[0] * 100:+.1f}c to {ci[1] * 100:+.1f}c' if ci else 'n/a (<5 sessions)') + f' | {flag} |')
    return L + ['']


def rank_coins(settled):
    pooled = sum(t['pnl'] > 0 for t in settled) / len(settled)
    rows = []
    for coin in sorted({t['asset'] for t in settled}):
        g = [t for t in settled if t['asset'] == coin]
        k = sum(t['pnl'] > 0 for t in g); cost = sum(t['cost'] for t in g) / sum(t['size'] for t in g)
        post = (k + SHRINK * pooled) / (len(g) + SHRINK)
        rows.append((post - cost, coin, g, k, cost, post))
    rows.sort(reverse=True)
    L = ['## 4. Coins ranked by calibrated net expected value', '',
         f'Win rate shrunk toward the pooled {pooled:.1%} with {SHRINK} pseudo-trades, so small samples cannot top the list by luck.', '',
         '| Rank | Coin | Trades | Raw win | Calibrated win | Cost/contract | Calibrated EV/contract | Realised/contract (session-clustered 95%) | Avg contracts |',
         '|---|---|---|---|---|---|---|---|---|']
    for i, (ev, coin, g, k, cost, post) in enumerate(rows, 1):
        ci = cluster_ci(g)
        L.append(f'| {i} | {coin} | {len(g)} | {k / len(g):.1%} | {post:.1%} | {cost * 100:.1f}c | {ev * 100:+.2f}c | {per_contract(g) * 100:+.2f}c '
                 + (f'({ci[0] * 100:+.1f} to {ci[1] * 100:+.1f})' if ci else '(n/a)') + f' | {sum(t["size"] for t in g) / len(g):.1f} |')
    return L + [''], [c for _, c, *_ in rows]


def policies(train, test, agree):
    """Each policy: (name, chosen parameter, selected test trades). Parameters are chosen on ``train`` only."""
    def one_per_session(ts):
        by = defaultdict(list)
        for t in ts:
            by[session_of(t)].append(t)
        return [min(v, key=lambda t: t['opened']) for v in by.values()]
    out = [('Existing strategy', '-', test), ('One market per session (first entry)', '-', one_per_session(test))]
    pooled = sum(t['pnl'] > 0 for t in train) / len(train) if train else 0
    best, best_ev = None, None
    for coin in {t['asset'] for t in train}:
        g = [t for t in train if t['asset'] == coin]
        ev = (sum(t['pnl'] > 0 for t in g) + SHRINK * pooled) / (len(g) + SHRINK) - sum(t['cost'] for t in g) / sum(t['size'] for t in g)
        if best_ev is None or ev > best_ev:
            best, best_ev = coin, ev
    out.append(('Single best coin (from first half)', best or '-', [t for t in test if t['asset'] == best]))
    if agree:
        a_train = [t for t in train if agree.get(id(t)) is not None]
        def keep(ts, th):
            return [t for t in ts if agree.get(id(t)) is not None and agree[id(t)] >= th - 1e-9]
        scored = [(per_contract(keep(a_train, th)) or -1, th) for th in THRESHOLDS if len(keep(a_train, th)) >= 20]
        th = max(scored)[1] if scored else THRESHOLDS[1]
        out.append((f'Agreement filter (>= {th:.0%} at entry)', f'{th:.0%}', keep(test, th)))
        out.append((f'Agreement >= {th:.0%} + one per session', f'{th:.0%}', one_per_session(keep(test, th))))
    return out


def compare(settled, agree):
    order = sorted({session_of(t) for t in settled}, key=lambda k: min(t['opened'] for t in settled if session_of(t) == k))
    first = set(order[:len(order) // 2])
    train = [t for t in settled if session_of(t) in first]; test = [t for t in settled if session_of(t) not in first]
    L = ['## 5. Out-of-sample comparison (chosen on the first half of the sessions, scored on the second half)', '',
         f'{len(train)} training trades ({len(first)} sessions) · {len(test)} test trades ({len(order) - len(first)} sessions).', '']
    if len(test) < 20:
        return L + ['Too few test trades for a comparison.', '']
    base_winners = {id(t) for t in test if t['pnl'] > 0}
    L += ['| Policy | Trades | Net | Net at $10/trade | Avg win | Avg loss | Profit factor | Per contract (session-clustered 95%) | Worst session | Max drawdown | Missed winners vs existing |',
          '|---|---|---|---|---|---|---|---|---|---|---|']
    for name, _, sel in policies(train, test, agree):
        m = metrics(sel); m10 = metrics(sel, value=scaled)
        if not m:
            L.append(f'| {name} | 0 | | | | | | | | | |'); continue
        ci = cluster_ci(sel); chosen = {id(t) for t in sel}
        missed = [t for t in test if id(t) in base_winners and id(t) not in chosen]
        L.append(f'| {name} | {m["n"]} | ${m["net"]:+,.2f} | ${m10["net"]:+,.2f} | ${m["avg_win"]:.2f} | ${m["avg_loss"]:.2f} | {fmt_pf(m["pf"])} | '
                 f'{m["pc"] * 100:+.2f}c ' + (f'({ci[0] * 100:+.1f} to {ci[1] * 100:+.1f})' if ci else '(n/a)') +
                 f' | ${m["worst_session"]:+,.2f} | ${m["dd"]:,.2f} | {len(missed)} (${sum(t["pnl"] for t in missed):,.2f}) |')
    if not agree:
        L += ['', 'Agreement policies need Kalshi history (run without --no-history).']
    L += ['', 'A policy is better only if its per-contract interval is clearly above the existing strategy\'s and it holds on new sessions (section 6).', '']
    return L


def versions(trades, state):
    vers = state.get('config_versions', {})
    by = defaultdict(list)
    for t in trades:
        by[t['raw'][0].get('cfg', 'untagged')].append(t)
    L = ['## 6. Settings versions (forward test)', '', '| Label | Trades | Sessions | Net | Per contract (session-clustered 95%) | Key settings |', '|---|---|---|---|---|---|']
    for v, ts in sorted(by.items(), key=lambda kv: min(t['opened'] for t in kv[1])):
        m = metrics(ts); ci = cluster_ci(ts); st = vers.get(v, {}).get('settings') or {}
        keys = ', '.join(f'{k}={st[k]}' for k in ('entry_min', 'entry_max', 'window', 'dollars', 'max_session_entries', 'max_trade_risk', 'agreement_min', 'direction_majority') if k in st)
        L.append(f'| {v} | {m["n"]} | {m["sessions"]} | ${m["net"]:+,.2f} | {m["pc"] * 100:+.2f}c ' + (f'({ci[0] * 100:+.1f} to {ci[1] * 100:+.1f})' if ci else '(n/a)') + f' | {keys} |')
    return L + ['', 'Promote a filter only when its own label shows a better per-contract interval than the label before it, on sessions it was not designed on.']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'), default='shadow')
    ap.add_argument('--since', default='')
    ap.add_argument('--db', default=os.path.join(ROOT, 'data', 'paper.sqlite3'))
    ap.add_argument('--cache', default=os.path.join(ROOT, 'output', 'cache', 'candles'))
    ap.add_argument('--offline', action='store_true'); ap.add_argument('--no-history', action='store_true'); ap.add_argument('--out')
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        sys.exit(f'Database not found: {a.db} (run this from the bot folder, e.g. cd ~/kalshi)')
    since = datetime.strptime(a.since, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() if a.since else 0
    state = r.load_state(a.db)
    raw_rows = [t for t in state['trades'] if t.get('mode', 'paper') == a.mode and t['opened'] >= since]
    trades = [t for t in r.group_trades(raw_rows)]
    for t in trades:
        t['asset'] = t.get('asset') or t['ticker'][2:].split('15M')[0]
    settled = [t for t in trades if t['reasons'] == ['settlement']]
    agree, with_hist = {}, 0
    if not a.no_history:
        print(f'{len(settled)} settled {a.mode} trades; loading Kalshi 1-minute history (cached)...', file=sys.stderr, flush=True)
        rows, _ = ss.study(trades, a.cache, a.offline, log=lambda *x, **k: print(*x, **k))
        with_hist = len(rows)
        agree = {id(x['t']): x.get('coins_agree_entry') for x in rows}
    L = [f'# Strategy lab: {a.mode.upper()} ledger', '', (f'Trades opened since {a.since}. ' if a.since else '') +
         f'Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Simulated ledger; not evidence of live profitability. Live trading stays disabled.', '']
    L += reconcile(raw_rows, trades, settled, with_hist, getattr(ss.study, 'skipped_early', 0))
    if not trades:
        print('\n'.join(L + ['No closed trades.'])); return
    L += breakdowns(trades) + (breakeven(settled) if settled else [])
    if settled:
        rk, _ = rank_coins(settled); L += rk
        L += compare(settled, agree)
    L += versions(trades, state)
    text = '\n'.join(L)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, 'w') as f:
            f.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
