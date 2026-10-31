"""Trading performance report (read-only) for one ledger: shadow, paper or live.

    .venv/bin/python pnlreport.py --mode shadow [--period all|today|24h|7d|mtd|YYYY-MM-DD:YYYY-MM-DD] [--out FILE]

Method (pnl-report skill, adapted to this app's data model):
* A closed trade is one position from entry to its final exit. Partial exits of the same
  position (same mode, market and entry time) are combined; open positions are never counted.
* Realized P&L is the app's recorded net P&L after fees. Settlement pays $1/$0 with no fee.
* Equity: SHADOW and LIVE use the app's ledger definition ($1,000 start + realized, open
  positions marked at their last bid); PAPER uses the recorded paper equity curve. The drawdown
  is measured on equity after each closed trade (marks between trades are not stored).
* The database is opened read-only (SQLite URI mode=ro); nothing is written to it.
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START_EQUITY = 1000.0


def load_state(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        return json.loads(con.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
    finally:
        con.close()


def period_bounds(period, now):
    day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'all':
        return None, None, 'all available history'
    if period == 'today':
        return day.timestamp(), None, 'today (UTC)'
    if period == '24h':
        return now - 86400, None, 'last 24 hours'
    if period == '7d':
        return now - 7 * 86400, None, 'last 7 days'
    if period == 'mtd':
        return day.replace(day=1).timestamp(), None, 'month to date (UTC)'
    a, b = period.split(':')
    start = datetime.fromisoformat(a).replace(tzinfo=timezone.utc).timestamp()
    end = (datetime.fromisoformat(b).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp()
    return start, end, f'{a} to {b} (UTC)'


def market_close(ticker):
    """Market close time from a 15-minute ticker like KXBTC15M-26OCT311530-30 (US Eastern)."""
    try:
        stamp = ticker.split('-')[1]
        local = datetime.strptime(stamp, '%y%b%d%H%M')
    except (IndexError, ValueError):
        return None
    from zoneinfo import ZoneInfo
    return local.replace(tzinfo=ZoneInfo('America/New_York')).timestamp()


def group_trades(rows):
    """Combine partial exits into one closed trade per position."""
    groups = defaultdict(list)
    for t in rows:
        groups[(t.get('mode', 'paper'), t['ticker'], t['opened'])].append(t)
    out = []
    for (mode, ticker, opened), parts in groups.items():
        parts.sort(key=lambda t: t['closed'])
        size = sum(t['size'] for t in parts)
        out.append(dict(mode=mode, ticker=ticker, asset=parts[0].get('asset') or ticker[2:].split('15M')[0], side=parts[0]['side'],
                        entry=parts[0]['entry'], size=size, cost=sum(t['cost'] for t in parts), pnl=sum(t['pnl'] for t in parts),
                        exit=sum(t['exit'] * t['size'] for t in parts) / size if size else None, opened=opened, closed=parts[-1]['closed'],
                        reasons=sorted({t['reason'] for t in parts}), parts=len(parts), raw=parts))
    return sorted(out, key=lambda t: t['closed'])


def quality(rows, all_trades):
    issues, excluded = [], []
    for t in rows:
        why = []
        if t.get('pnl') is None: why.append('missing P&L')
        if t.get('exit') is None or t.get('reason') is None or t.get('closed') is None: why.append('missing exit data')
        if t.get('closed') is not None and t.get('opened') is not None and t['closed'] < t['opened'] - 1: why.append('closed before opened')
        if t.get('size', 0) <= 0 or t.get('cost', 0) <= 0: why.append('non-positive size or cost')
        if t.get('pnl') is not None and t.get('exit') is not None:
            calc = t['exit'] * t['size'] - t.get('exit_fee', 0) - t['cost']
            if abs(calc - t['pnl']) > 0.011: why.append(f'P&L {t["pnl"]:.2f} does not reconcile with exit-fees-cost {calc:.2f}')
        if t.get('reason') == 'settlement' and t.get('exit') not in (0.0, 1.0): why.append('settlement price not $0/$1')
        if why:
            excluded.append((t.get('ticker'), t.get('mode'), '; '.join(why)))
    seen = defaultdict(int)
    for t in all_trades:
        seen[(t.get('mode', 'paper'), t['ticker'], t['opened'], t['closed'], t['size'], t['exit'])] += 1
    dups = [k for k, v in seen.items() if v > 1]
    if dups: issues.append(f'{len(dups)} duplicate trade records')
    return issues, excluded


def metrics(trades):
    n = len(trades)
    if not n:
        return None
    wins = [t['pnl'] for t in trades if t['pnl'] > 1e-9]
    losses = [t['pnl'] for t in trades if t['pnl'] < -1e-9]
    gross_win, gross_loss = sum(wins), -sum(losses)
    win_rate, loss_rate = len(wins) / n, len(losses) / n
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    return dict(n=n, wins=len(wins), losses=len(losses), even=n - len(wins) - len(losses), win_rate=win_rate,
                pnl=sum(t['pnl'] for t in trades), avg_win=avg_win, avg_loss=avg_loss,
                best=max(t['pnl'] for t in trades), worst=min(t['pnl'] for t in trades),
                expectancy=win_rate * avg_win - loss_rate * avg_loss,
                profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None, cost=sum(t['cost'] for t in trades))


def equity_curve(mode, s, closed_all):
    """[(time, equity)] after each closed trade, starting at $1,000."""
    if mode == 'paper' and s.get('curve'):
        return [(p['t'], p['value']) for p in s['curve']]
    eq, out = START_EQUITY, [(closed_all[0]['opened'] if closed_all else 0, START_EQUITY)]
    for t in closed_all:
        eq += t['pnl']; out.append((t['closed'], eq))
    return out


def drawdown(curve):
    peak, worst, worst_pct = curve[0][1] if curve else 0, 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        worst = max(worst, peak - v)
        if peak > 0: worst_pct = max(worst_pct, (peak - v) / peak)
    return peak, worst, worst_pct


def bucket_table(trades, key, order=None):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    names = order or sorted(groups)
    lines = ['| Group | Trades | Win rate | Avg entry | Net P&L | P&L per trade |', '|---|---|---|---|---|---|']
    for name in names:
        g = groups.get(name)
        if not g: continue
        m = metrics(g)
        lines.append(f"| {name} | {m['n']} | {m['win_rate']:.1%} | {sum(t['entry'] for t in g)/len(g)*100:.1f}¢ | {m['pnl']:+.2f} | {m['pnl']/m['n']:+.2f} |")
    return lines


def entry_bucket(t):
    e = t['entry']
    for lo, hi in ((0, .70), (.70, .75), (.75, .80), (.80, .85), (.85, .90), (.90, .93), (.93, 1.01)):
        if lo <= e < hi:
            return f'{lo*100:.0f}–{min(hi, 1)*100:.0f}¢'


def time_bucket(t):
    close = market_close(t['ticker'])
    if close is None: return 'unknown'
    left = (close - t['opened']) / 60
    return '<1 min' if left < 1 else '1–3 min' if left < 3 else '3–5 min' if left < 5 else '5–8 min' if left < 8 else '8+ min'


def comparison(s, mode, other):
    """Same markets traded by both ledgers: how fills and results differed."""
    grouped = [t for t in group_trades(s['trades']) if t['mode'] in (mode, other)]
    starts = [min((t['opened'] for t in grouped if t['mode'] == md), default=None) for md in (mode, other)]
    if None in starts:
        return None
    since = max(starts)                       # only the period when both ledgers were trading
    by = defaultdict(dict)
    for t in grouped:
        if t['opened'] >= since - 60:
            by[t['ticker']][t['mode']] = t
    both = [v for v in by.values() if mode in v and other in v]
    only_other = [v[other] for v in by.values() if other in v and mode not in v]   # e.g. shadow orders that found no fill
    only_mode = [v[mode] for v in by.values() if mode in v and other not in v]
    if not both:
        return None
    d_entry = [v[mode]['entry'] - v[other]['entry'] for v in both]
    d_pc = [v[mode]['pnl'] / v[mode]['size'] - v[other]['pnl'] / v[other]['size'] for v in both]
    return dict(n=len(both), entry=sum(d_entry) / len(d_entry), pnl_pc=sum(d_pc) / len(d_pc),
                worse=sum(1 for x in d_entry if x > 1e-9), better=sum(1 for x in d_entry if x < -1e-9),
                only_other=len(only_other), only_other_pnl=sum(t['pnl'] for t in only_other),
                only_other_wr=sum(t['pnl'] > 0 for t in only_other) / len(only_other) if only_other else None,
                only_mode=len(only_mode), only_mode_pnl=sum(t['pnl'] for t in only_mode))


def report(s, mode, period, now):
    start, end, label = period_bounds(period, now)
    all_rows = [t for t in s['trades'] if t.get('mode', 'paper') == mode]
    issues, excluded = quality(all_rows, s['trades'])
    bad = {(tk, md) for tk, md, _ in excluded}
    rows = [t for t in all_rows if (t['ticker'], t.get('mode', 'paper')) not in bad]
    closed_all = group_trades(rows)
    trades = [t for t in closed_all if (start is None or t['closed'] >= start) and (end is None or t['closed'] < end)]
    book = s.get('shadow_positions', {}) if mode == 'shadow' else s.get('positions', {})
    open_pos = [p for p in book.values() if p.get('mode', 'paper') == mode]
    m = metrics(trades)
    curve = equity_curve(mode, s, closed_all)
    unrealized = sum(((p.get('mark') if p.get('mark') is not None else p['entry']) * p['size'] - p['cost']) for p in open_pos)
    if mode == 'paper':
        current = s['cash'] + sum((p.get('mark') if p.get('mark') is not None else p['entry']) * p['size'] for p in open_pos)
    elif mode == 'live':
        current = None if s.get('live_balance') is None else s['live_balance'] + sum((p.get('mark') or p['entry']) * p['size'] for p in open_pos)
    else:
        current = START_EQUITY + sum(t['pnl'] for t in closed_all) + unrealized
    in_period = [(t, v) for t, v in curve if (start is None or t >= start) and (end is None or t < end)]
    peak, dd, dd_pct = drawdown(in_period or curve)
    first = (in_period or curve)[0][1] if curve else START_EQUITY
    na = 'N/A'
    L = [f'# {mode.upper()} P&L report', '', f'Generated {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M} UTC from `data/paper.sqlite3` (read-only).', '',
         '## PnL Report', f'- **Period:** {label}', f'- **Closed trades:** {m["n"] if m else 0}',
         f'- **Open positions:** {len(open_pos)}' + (f' (unrealized {unrealized:+.2f}, at last bid, not included below)' if open_pos else ''), '',
         '## Performance']
    if m:
        L += [f'- **Win rate:** {m["win_rate"]:.1%} ({m["wins"]} won, {m["losses"]} lost, {m["even"]} breakeven)',
              f'- **Realized P&L:** ${m["pnl"]:+,.2f}',
              f'- **Realized P&L %:** {m["pnl"]/START_EQUITY:+.2%} of the ${START_EQUITY:,.0f} starting balance; {m["pnl"]/m["cost"]:+.2%} of the ${m["cost"]:,.2f} put at risk',
              f'- **Average win / average loss:** ${m["avg_win"]:.2f} / -${m["avg_loss"]:.2f} (largest ${m["best"]:+.2f} / ${m["worst"]:+.2f})',
              f'- **Expectancy:** ${m["expectancy"]:+.2f} per trade',
              f'- **Profit factor:** {m["profit_factor"]:.2f}' if m['profit_factor'] is not None else '- **Profit factor:** N/A (no losing trades)',
              f'- **Break-even win rate at the average entry:** {sum(t["cost"] for t in trades)/sum(t["size"] for t in trades):.1%} (cost per contract incl. fees)']
    else:
        L += ['- No closed trades in this period: every metric is N/A.']
    L += ['', '## Risk' + (' (equity after each closed trade; open positions at last bid)' if mode != 'paper' else ' (recorded paper equity curve)'),
          f'- **Starting equity:** ${first:,.2f}' + (' (start of period)' if start else ''),
          '- **Current equity:** ' + (f'${current:,.2f}' if current is not None else na + ' (live balance not read yet)'),
          f'- **Peak equity:** ${peak:,.2f}',
          '- **Return:** ' + (f'{(current/first-1):+.2%}' if current is not None and first else na),
          f'- **Maximum drawdown:** ${dd:,.2f} ({dd_pct:.2%})', '']
    if m:
        cmp_with = 'paper' if mode != 'paper' else 'shadow'
        c = comparison(s, mode, cmp_with)
        L += ['## Breakdown', '', '### By entry price', *bucket_table(trades, entry_bucket, ['0–70¢', '70–75¢', '75–80¢', '80–85¢', '85–90¢', '90–93¢', '93–100¢']), '',
              '### By time left at entry', *bucket_table(trades, time_bucket, ['<1 min', '1–3 min', '3–5 min', '5–8 min', '8+ min', 'unknown']), '',
              '### By coin', *bucket_table(trades, lambda t: t['asset']), '',
              '### By side', *bucket_table(trades, lambda t: t['side'].upper()), '',
              '### By exit', *bucket_table(trades, lambda t: '+'.join(t['reasons'])), '']
        if c:
            L += [f'### {mode.upper()} vs {cmp_with.upper()} on the same {c["n"]} markets',
                  f'- Average entry difference: {c["entry"]*100:+.2f}¢ per contract ({c["worse"]} worse, {c["better"]} better, rest equal)',
                  f'- Average P&L difference: {c["pnl_pc"]*100:+.2f}¢ per contract',
                  f'- Markets only {cmp_with.upper()} traded: {c["only_other"]}' + (f' ({cmp_with} P&L {c["only_other_pnl"]:+.2f}, win rate {c["only_other_wr"]:.1%}). '
                  f'For SHADOW these are mostly orders that missed because the price moved before the simulated 250 ms arrival, the cost of a delayed IOC order.' if c['only_other'] else ''),
                  f'- Markets only {mode.upper()} traded: {c["only_mode"]}' + (f' (P&L {c["only_mode_pnl"]:+.2f})' if c['only_mode'] else ''),
                  '- The entry comparison covers only markets both ledgers filled, so it shows the price difference when filled, not the cost of misses.', '']
    L += ['## Data Quality', f'- Anomalies found: {"; ".join(issues) if issues else "none"}',
          f'- Open positions excluded from realized metrics: {len(open_pos)}',
          f'- Records excluded from calculations: {len(excluded)}']
    L += [f'  - {tk} ({md}): {why}' for tk, md, why in excluded[:20]]
    L += ['- Partial exits combined into whole trades: ' + str(sum(1 for t in trades if t['parts'] > 1)),
          '- Assumptions: fees are the app\'s recorded fees (Kalshi-reported for live, estimated for paper/shadow); paper and shadow results are simulations, not evidence of live performance.']
    return '\n'.join(L) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'), default='shadow')
    ap.add_argument('--period', default='all')
    ap.add_argument('--db', default=str(ROOT / 'data' / 'paper.sqlite3'))
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    import time
    text = report(load_state(a.db), a.mode, a.period, time.time())
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
    sys.stdout.write(text)


if __name__ == '__main__':
    main()
