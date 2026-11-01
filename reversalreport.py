"""Read-only: would selling at each reversal signal have beaten holding to settlement?
Joins `reversal_signal` audit events with the settled trades of the same position.
Usage: .venv/bin/python reversalreport.py [--db data/paper.sqlite3] [--log data/trade_log.jsonl]"""
import argparse
import json
import os
import sqlite3
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from settings import fee  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument('--db', default='data/paper.sqlite3'); ap.add_argument('--log', default='data/trade_log.jsonl')
a = ap.parse_args()
s = json.loads(sqlite3.connect(f'file:{a.db}?mode=ro', uri=True).execute('SELECT body FROM state WHERE id=1').fetchone()[0])
signals = {}
with open(a.log) as f:
    for line in f:
        if '"reversal_signal"' not in line: continue
        d = json.loads(line)
        if d.get('event') == 'reversal_signal': signals.setdefault((d['mode'], d['ticker']), d)   # first signal per position
held = {}
for t in s['trades']:
    k = (t.get('mode', 'paper'), t['ticker'])
    held.setdefault(k, []).append(t)
rows = []
for k, sig in signals.items():
    parts = held.get(k)
    if not parts or sig.get('bid') is None: continue
    size = sig['size']; bid = sig['bid']
    sell = bid * size - fee(bid, size)                               # proceeds selling everything at the signal's bid
    hold = sum(p['exit'] * p['size'] - p.get('exit_fee', 0) for p in parts)   # what the position actually returned
    rows.append((k, sig, sell, hold, any(p['reason'] == 'reversal exit' for p in parts)))
print(f'{len(signals)} reversal signals; {len(rows)} matched to closed positions.\n')
if rows:
    print(f'{"mode":6} {"ticker":28} {"side":4} {"bid at signal":>13} {"sell value":>10} {"actual":>8}  better')
    for (mode, tk), sig, sell, hold, acted in rows[-25:]:
        print(f'{mode:6} {tk:28} {sig["side"].upper():4} {sig["bid"]*100:12.1f}c {sell:+10.2f} {hold:+8.2f}  {"SELL" if sell > hold else "HOLD"}{"  (sold)" if acted else ""}')
    gain = sum(sell - hold for *_, sell, hold, _ in rows)
    wins = sum(sell > hold for *_, sell, hold, _ in rows)
    print(f'\nSelling at the signal would have been better in {wins} of {len(rows)} positions; total difference {gain:+.2f} '
          f'({"selling helps" if gain > 0 else "holding was better"}). Positions already sold by the reversal exit show what they got; '
          f'"sell value" uses the bid at the first signal.')
