"""Read-only report on the final-minute monitor (data/final_minute.jsonl).

1. Calibration: when the model said the leading side wins X% of the time, how often did it?
2. Opportunities: the first moment in each market's last minute when a side's ask was below the
   model's probability by at least the edge (after the estimated fee); how those trades would
   have settled, per contract, with the size that was on the book.
3. Close races: the leading side priced at 70c or less with 10 s or less to go.
Usage: .venv/bin/python finalminutereport.py [--file data/final_minute.jsonl]"""
import argparse
import json
import os
import sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from settings import fee  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument('--file', default='data/final_minute.jsonl'); a = ap.parse_args()
snaps, result = defaultdict(list), {}
with open(a.file) as f:
    for line in f:
        try: d = json.loads(line)
        except ValueError: continue
        if d.get('kind') == 'snap': snaps[d['ticker']].append(d)
        elif d.get('kind') == 'outcome' and d.get('result'): result[d['ticker']] = d['result']
done = {tk: sorted(v, key=lambda d: d['t']) for tk, v in snaps.items() if tk in result}
print(f'{len(snaps)} markets recorded, {len(done)} with a known outcome, {sum(len(v) for v in done.values())} snapshots.\n')
if not done: sys.exit(0)

print('1. CALIBRATION (leading side by the model, every snapshot)')
cal = defaultdict(lambda: [0, 0])
for tk, v in done.items():
    for d in v:
        if not d.get('model'): continue
        p = d['model']['p_yes']; side, pl = ('yes', p) if p >= .5 else ('no', 1 - p)
        b = '99%+' if pl >= .99 else '97-99%' if pl >= .97 else '90-97%' if pl >= .90 else '70-90%' if pl >= .70 else '50-70%'
        cal[b][0] += 1; cal[b][1] += result[tk] == side
for b in ('50-70%', '70-90%', '90-97%', '97-99%', '99%+'):
    n, w = cal[b]
    if n: print(f'   model {b:7s}: {n:6d} snapshots, actually won {w / n:6.1%}')
print('   (close agreement means the model can be trusted for the sections below)\n')

def per_contract(ask, won):
    return (1 if won else 0) - ask - fee(ask, 10) / 10

print('2. OPPORTUNITIES (first qualifying moment per market; one entry per market)')
for edge in (.02, .05, .10):
    rows = []
    for tk, v in done.items():
        for d in v:
            m = d.get('model')
            if not m: continue
            for side, p in (('yes', m['p_yes']), ('no', 1 - m['p_yes'])):
                ask, size = d.get(side + '_ask'), d.get(side + '_ask_size') or 0
                if ask is None or size < 1 or ask >= 1: continue
                if p - ask - fee(ask, 10) / 10 >= edge:
                    rows.append((tk, d, side, ask, size, p)); break
            else: continue
            break
    if not rows:
        print(f'   edge >= {edge*100:.0f}c: none'); continue
    pnl = [per_contract(ask, result[tk] == side) for tk, d, side, ask, size, p in rows]
    wins = sum(result[tk] == side for tk, d, side, *_ in rows)
    sizes = sorted(r[4] for r in rows)
    print(f'   edge >= {edge*100:.0f}c: {len(rows)} markets, won {wins / len(rows):.1%}, avg ask {sum(r[3] for r in rows) / len(rows) * 100:.1f}c, '
          f'avg {sum(r[1]["sec_left"] for r in rows) / len(rows):.0f} s left, median size on book {sizes[len(sizes) // 2]:g}, '
          f'P&L per contract {sum(pnl) / len(pnl) * 100:+.2f}c (total {sum(pnl):+.2f} per 1 contract each)')
    for lo, hi in ((0, .70), (.70, .90), (.90, .97), (.97, 1)):
        g = [(r, x) for r, x in zip(rows, pnl, strict=True) if lo <= r[3] < hi]
        if g: print(f'      ask {lo*100:.0f}-{hi*100:.0f}c: {len(g):4d} markets, won {sum(result[r[0]] == r[2] for r, _ in g) / len(g):6.1%}, per contract {sum(x for _, x in g) / len(g) * 100:+6.2f}c')
print()
print('3. CLOSE RACES: leader by price at 70c or less with 10 s or less left (first such moment per market)')
rows = []
for tk, v in done.items():
    for d in v:
        if d['sec_left'] > 10: continue
        ya, na = d.get('yes_ask'), d.get('no_ask')
        if ya is None or na is None: continue
        side, ask = ('yes', ya) if ya >= na else ('no', na)
        if ask <= .70: rows.append((tk, side, ask, d)); break
if rows:
    pnl = [per_contract(ask, result[tk] == side) for tk, side, ask, d in rows]
    print(f'   {len(rows)} markets, leader won {sum(result[tk] == s for tk, s, *_ in rows) / len(rows):.1%}, avg ask {sum(r[2] for r in rows) / len(rows) * 100:.1f}c, '
          f'P&L per contract {sum(pnl) / len(pnl) * 100:+.2f}c')
else:
    print('   none')
