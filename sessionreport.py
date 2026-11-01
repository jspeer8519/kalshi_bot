"""Read-only: per-session view of settled trades. Are losses spread at random across coins, or is
there a pattern (side against the other coins, entry price, coin)?
Usage: .venv/bin/python sessionreport.py [--since YYYY-MM-DD] [--db data/paper.sqlite3]"""
import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
ap = argparse.ArgumentParser(); ap.add_argument('--db', default='data/paper.sqlite3'); ap.add_argument('--since', default='')
a = ap.parse_args()
since = datetime.strptime(a.since, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() if a.since else 0
db = a.db
s = json.loads(sqlite3.connect(f'file:{db}?mode=ro', uri=True).execute('SELECT body FROM state WHERE id=1').fetchone()[0])
for mode in ('paper', 'shadow'):
    T = [t for t in s['trades'] if t.get('mode', 'paper') == mode and t['closed'] >= since and t['reason'] == 'settlement']
    S = defaultdict(list)
    for t in T: S[t['ticker'].split('-')[1]].append(t)          # same session code (e.g. 26NOV010230) across coins
    multi = {k: v for k, v in S.items() if len(v) >= 2}
    if not multi: continue
    print(f'\n== {mode.upper()}: {len(T)} settled trades in {len(S)} sessions ({len(multi)} with 2+ coins) ==')
    with_loss = [v for v in multi.values() if any(t['pnl'] < 0 for t in v)]
    n = sum(len(v) for v in multi.values()); w = sum(t['pnl'] > 0 for v in multi.values() for t in v)
    p = w / n; exp = sum(1 - p ** len(v) for v in multi.values())
    print(f'  sessions with at least one loss: {len(with_loss)} of {len(multi)} (expected ~{exp:.0f} if each coin wins {p:.0%} independently)')
    sides = defaultdict(lambda: [0, 0, 0.0]); price = defaultdict(lambda: [0, 0, 0.0]); coin = defaultdict(lambda: [0, 0, 0.0])
    for v in multi.values():
        yes = sum(t['side'] == 'yes' for t in v); maj = 'yes' if yes * 2 > len(v) else 'no' if yes * 2 < len(v) else None
        for t in v:
            k = 'tie' if maj is None else ('with majority' if t['side'] == maj else 'AGAINST majority')
            for d, key in ((sides, k), (price, '80-84c' if t['entry'] < .845 else '85-88c' if t['entry'] < .885 else '89-92c' if t['entry'] < .925 else '93c+'), (coin, t.get('asset', '?'))):
                d[key][0] += 1; d[key][1] += t['pnl'] > 0; d[key][2] += t['pnl']
    for title, d in (('side vs the other coins that session', sides), ('entry price', price), ('coin', coin)):
        print(f'  by {title}:')
        for k, (c, wn, pl) in sorted(d.items(), key=lambda x: -x[1][0]):
            print(f'     {k:18s} {c:4d} trades  win {wn / c:6.1%}  net {pl:+8.2f}')
    by_ver = defaultdict(list); by_hour = defaultdict(list)
    for t in T:
        by_ver[t.get('cfg', 'untagged')].append(t); by_hour[datetime.fromtimestamp(t['opened'], timezone.utc).hour].append(t)
    vers = s.get('config_versions', {})
    print('  by settings version (each trade is labelled with the settings that opened it; untagged = before labels existed):')
    prev = None
    for v, g in sorted(by_ver.items(), key=lambda x: min(t['opened'] for t in x[1])):
        c = sum(t['size'] for t in g) or 1
        info = vers.get(v, {}).get('settings')
        diff = '' if not info else (' · ' + ', '.join(f'{k}={info[k]}' for k in ('entry_min', 'entry_max', 'window', 'dollars', 'max_same_side', 'direction_majority', 'max_session_risk', 'bb_filter', 'reversal_exit') if k in info and (prev is None or prev.get(k) != info[k])))
        print(f"     {v:9s} {len(g):4d} trades  win {sum(t['pnl'] > 0 for t in g) / len(g):6.1%}  net {sum(t['pnl'] for t in g):+8.2f}  per contract {sum(t['pnl'] for t in g) / c * 100:+6.2f}c{diff}")
        prev = info or prev
    print('  by hour of entry (UTC; 12:30-13:30 UTC = 8:30-9:30 ET, US data releases and stock market open):')
    for h in sorted(by_hour):
        g = by_hour[h]
        print(f"     {h:02d}:00  {len(g):4d} trades  win {sum(t['pnl'] > 0 for t in g) / len(g):6.1%}  net {sum(t['pnl'] for t in g):+8.2f}")
    print('  last 8 multi-coin sessions:')
    for k in sorted(multi, key=lambda k: max(t['closed'] for t in multi[k]))[-8:]:
        v = multi[k]
        print(f"     {datetime.fromtimestamp(max(t['closed'] for t in v), timezone.utc):%m-%d %H:%M}  " + ' '.join(f"{t.get('asset','?')}:{t['side'][0].upper()}{'+' if t['pnl'] > 0 else 'X'}" for t in sorted(v, key=lambda t: t.get('asset',''))) + f"   net {sum(t['pnl'] for t in v):+.2f}")
