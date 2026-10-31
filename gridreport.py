"""Read-only: which entry range x entry window would have done best on the recorded trades.
Usage: .venv/bin/python gridreport.py [--mode paper|shadow] [--min 40]"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
from datetime import datetime, timezone
import pnlreport as r
ap = argparse.ArgumentParser(); ap.add_argument('--mode', default='paper'); ap.add_argument('--min', type=int, default=40)
ap.add_argument('--db', default='data/paper.sqlite3'); a = ap.parse_args()
s = r.load_state(a.db)
T = [t for t in r.group_trades([t for t in s['trades'] if t.get('mode', 'paper') == a.mode]) if r.market_close(t['ticker'])]
for t in T: t['left'] = (r.market_close(t['ticker']) - t['opened']) / 60
T.sort(key=lambda t: t['opened']); half = T[len(T) // 2]['opened'] if T else 0
def stats(g):
    n = len(g); c = sum(t['size'] for t in g)
    if not n or not c: return None
    return dict(n=n, win=sum(t['pnl'] > 0 for t in g) / n, net=sum(t['pnl'] for t in g), pc=sum(t['pnl'] for t in g) / c * 100)
rows = []
for lo in (.70, .75, .78, .80, .82, .84, .85):
    for hi in (.88, .90, .92, .94, .96):
        if hi <= lo: continue
        for w in (2, 3, 4, 5, 6, 7, 8, 10, 15):
            g = [t for t in T if lo <= t['entry'] <= hi and t['left'] <= w]
            st = stats(g)
            if not st or st['n'] < a.min: continue
            e, l = stats([t for t in g if t['opened'] < half]), stats([t for t in g if t['opened'] >= half])
            rows.append((lo, hi, w, st, e, l))
print(f'{a.mode.upper()}: {len(T)} closed trades; first half before {datetime.fromtimestamp(half, timezone.utc):%m-%d %H:%M} UTC. Groups with fewer than {a.min} trades hidden.')
print('per-contract = net P&L / contracts (after fees). "both halves" = profitable in the earlier AND the later half (guards against luck).\n')
hdr = f'{"entry":>11} {"window":>6} {"trades":>6} {"win":>6} {"net $":>8} {"per-c":>7}  {"1st half":>8} {"2nd half":>8}  both halves'
def line(x):
    lo, hi, w, st, e, l = x
    f = lambda v: f'{v["pc"]:+7.2f}c' if v else '    n/a '
    ok = bool(e and l and e['pc'] > 0 and l['pc'] > 0)
    return f'{lo:.2f}-{hi:.2f} {w:>4}m {st["n"]:>6} {st["win"]:6.1%} {st["net"]:+8.2f} {st["pc"]:+6.2f}c  {f(e)} {f(l)}  {"YES" if ok else "no"}'
print('TOP 12 BY TOTAL NET P&L'); print(hdr); [print(line(x)) for x in sorted(rows, key=lambda x: -x[3]['net'])[:12]]
print('\nTOP 12 BY PER-CONTRACT P&L'); print(hdr); [print(line(x)) for x in sorted(rows, key=lambda x: -x[3]['pc'])[:12]]
print('\nMOST TRADES THAT STAY >= +1.0c PER CONTRACT IN BOTH HALVES')
print(hdr); [print(line(x)) for x in sorted([x for x in rows if x[4] and x[5] and x[4]['pc'] >= 1 and x[5]['pc'] >= 1], key=lambda x: -x[3]['n'])[:12]]
print('\nBY TIME LEFT (entry >= 0.80), each slice on its own')
for w0, w1 in ((0, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 8), (8, 15)):
    st = stats([t for t in T if t['entry'] >= .80 and w0 < t['left'] <= w1])
    if st: print(f'  {w0}-{w1} min: {st["n"]:4d} trades  win {st["win"]:6.1%}  per-contract {st["pc"]:+6.2f}c  net {st["net"]:+8.2f}')
print('\nBY EXIT TYPE (entry >= 0.80, <= 5 min)')
for ex in sorted({'+'.join(t['reasons']) for t in T}):
    st = stats([t for t in T if '+'.join(t['reasons']) == ex and t['entry'] >= .80 and t['left'] <= 5])
    if st: print(f'  {ex:24s} {st["n"]:4d} trades  per-contract {st["pc"]:+6.2f}c  net {st["net"]:+8.2f}')
