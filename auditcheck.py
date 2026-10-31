"""Read-only health and accuracy audit of the running Kalshi Desk.

    .venv/bin/python auditcheck.py [--out FILE] [--no-network]

Checks the running app (process, API, feeds, mode, halts), accounting (paper cash reconciles
with recorded P&L), every recorded trade, recent settlements against Kalshi's official results,
the chart's 60-second average against Kalshi's official settlement values, the hash-chained
audit log, secret-file permissions and process memory. Nothing is changed: the database is
opened read-only, the app is only queried, and credentials are never read (only their file
permissions are). Exit status 1 if any check FAILs, so schedulers can flag it."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import audit
import pnlreport
from settings import timestamp, validate

ROOT = Path(__file__).resolve().parent
APP = 'http://127.0.0.1:8765'
KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
SECRET_FILES = ('.env', 'kalshi_private_key.pem', '.private/credentials.json')


def get(url, timeout=10):
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'KalshiDesk-audit/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Audit:
    def __init__(self):
        self.rows = []

    def add(self, status, check, detail):
        self.rows.append((status, check, detail))

    def check(self, name, fn):
        try:
            fn()
        except Exception as e:                       # one failing check never stops the others
            self.add('FAIL', name, f'check could not run ({type(e).__name__}: {str(e)[:120]})')

    @property
    def failed(self):
        return any(s == 'FAIL' for s, _, _ in self.rows)

    def markdown(self):
        icon = dict(PASS='✅', WARN='⚠️', FAIL='❌')
        counts = {k: sum(1 for s, _, _ in self.rows if s == k) for k in ('PASS', 'WARN', 'FAIL')}
        lines = [f'# Kalshi Desk audit, {time.strftime("%Y-%m-%d %H:%M %Z")}', '',
                 f'**{counts["PASS"]} passed · {counts["WARN"]} warnings · {counts["FAIL"]} failed**', '', '| | Check | Result |', '|---|---|---|']
        lines += [f'| {icon[s]} | {c} | {d} |' for s, c, d in self.rows]
        return '\n'.join(lines) + '\n'


def run(network=True):
    A = Audit()
    s = pnlreport.load_state(ROOT / 'data' / 'paper.sqlite3')
    live = {}

    def app():
        st = get(APP + '/api/state', 5); live['state'] = st
        validate(dict(st['config']))
        flags = [x for x in ('live_halt', 'kill_active') if st.get(x)]
        A.add('WARN' if flags or st.get('pending_changed') else 'PASS', 'App and configuration',
              f'{st["mode"]}, {"running" if st["running"] else "PAUSED"}, config valid'
              + (f', active: {", ".join(flags)}' if flags else '') + (f', pending: {", ".join(st["pending_changed"])}' if st.get('pending_changed') else ''))
        if not st['running']:
            A.add('WARN', 'Entries', 'Engine is paused: press Start to trade')
    A.check('App and configuration', app)

    def feeds():
        st = live['state']
        ages = {a: round(st['now'] - m['fetched'], 1) for a, m in st['markets'].items()}
        stale = {a: x for a, x in ages.items() if x > 10}
        missing = [a for a, m in st['markets'].items() if m.get('status') == 'active' and m.get('strike') is None]
        A.add('FAIL' if stale else 'WARN' if missing or st['errors'] else 'PASS', 'Market feeds',
              f'{len(ages)} markets, oldest {max(ages.values(), default=0)} s' + (f'; STALE {stale}' if stale else '')
              + (f'; no target yet: {missing}' if missing else '') + (f'; errors {st["errors"]}' if st['errors'] else ''))
        ix = get(APP + f'/api/index?after={time.time() - 5}', 5)
        slow = [a for a, v in ix['index'].items() if not v.get('avg')]
        A.add('PASS' if ix['status'] == 'connected' and not slow else 'WARN', 'Settlement index stream',
              f'{ix["status"]}' + (f', error {ix["error"]}' if ix.get('error') else '') + (f'; no 60-s average in 5 s for {slow}' if slow else ', all coins updating'))
    A.check('Market feeds', feeds)

    def accounting():
        paper = [t for t in s['trades'] if t.get('mode', 'paper') == 'paper']
        open_cost = sum(p['cost'] for p in s['positions'].values() if p.get('mode', 'paper') == 'paper')
        expected = 1000 + sum(t['pnl'] for t in paper) - open_cost
        ok = abs(expected - s['cash']) < .01
        A.add('PASS' if ok else 'FAIL', 'Paper cash reconciliation', f'stored ${s["cash"]:.2f}, expected ${expected:.2f}')
        issues, excluded = pnlreport.quality(s['trades'], s['trades'])
        twice = {}
        for t in s['trades']:
            twice.setdefault((t.get('mode', 'paper'), t['ticker']), set()).add(t['opened'])
        doubles = [k for k, v in twice.items() if len(v) > 1]
        A.add('FAIL' if excluded or issues or doubles else 'PASS', 'Trade records',
              f'{len(s["trades"])} records' + ('; all reconcile' if not (excluded or issues or doubles) else
              f'; {len(excluded)} bad, {len(doubles)} markets entered twice {doubles[:3]}, {issues}'))
    A.check('Accounting', accounting)

    def settlements():
        recent = [t for t in s['trades'] if t['reason'] == 'settlement'][:40]
        bad = []
        for t in recent:
            m = get(f'{KALSHI}/markets/{t["ticker"]}')['market']
            if float(m.get('result') == t['side']) != t['exit']:
                bad.append(t['ticker'])
        A.add('FAIL' if bad else 'PASS', 'Settlements vs Kalshi', f'{len(recent)} checked, {len(bad)} mismatches {bad[:3] if bad else ""}')
    if network:
        A.check('Settlements vs Kalshi', settlements)

    def chart_accuracy():
        ix = get(APP + '/api/index?after=0', 20)
        out, worst = [], 0.0
        for a, v in sorted(ix['index'].items()):
            avg = {int(t): val for t, val in v['avg']}
            for m in get(f'{KALSHI}/markets?series_ticker=KX{a}15M&status=settled&limit=3')['markets']:
                close = int(timestamp(m['close_time']))
                ours = avg.get(close, avg.get(close - 1))
                if ours is None or not m.get('expiration_value'):
                    continue
                official = float(m['expiration_value'])
                tol = 10 ** -(len(m['expiration_value'].split('.')[-1]) if '.' in m['expiration_value'] else 0)   # Kalshi rounds its figure
                rel = abs(ours - official) / official
                worst = max(worst, rel)
                out.append((a, abs(ours - official) <= tol * .51 + 1e-9))
                break
        wrong = [a for a, ok in out if not ok]
        A.add('PASS' if out and not wrong else 'WARN' if not out else 'FAIL', 'Chart line vs official settlement',
              f'{len(out)} coins compared at their last close' + (f'; differ: {wrong}' if wrong else ', all equal to Kalshi\'s rounded value'))
    if network:
        A.check('Chart accuracy', chart_accuracy)

    def logs():
        ok, problem = audit.verify(str(ROOT / 'data' / 'trade_log.jsonl'))
        body = (ROOT / 'data' / 'trade_log.jsonl').read_text()
        leak = any(x in body for x in ('PRIVATE KEY', '-----BEGIN', 'KALSHI-ACCESS-SIGNATURE'))
        A.add('PASS' if ok and not leak else 'FAIL', 'Audit log', ('hash chain intact' if ok else f'chain BROKEN: {problem}')
              + ('; secret-like text FOUND' if leak else '; no secrets') + f'; {len(body.splitlines())} lines')
        loose = [f for f in SECRET_FILES if (ROOT / f).exists() and os.stat(ROOT / f).st_mode & 0o077]
        A.add('FAIL' if loose else 'PASS', 'Secret file permissions', 'owner-only' if not loose else f'readable by others: {loose} (run chmod 600)')
    A.check('Logs and secrets', logs)

    def process():
        pid = subprocess.run(['pgrep', '-f', 'python app.py'], capture_output=True, text=True).stdout.split()
        if not pid:
            A.add('FAIL', 'Process', 'app.py is not running'); return
        status = Path(f'/proc/{pid[0]}/status').read_text()
        rss = int(next(x for x in status.splitlines() if x.startswith('VmRSS')).split()[1]) / 1024
        threads = int(next(x for x in status.splitlines() if x.startswith('Threads')).split()[1])
        A.add('WARN' if rss > 400 else 'PASS', 'Process memory', f'{rss:.0f} MB, {threads} threads' + (' (high: check after a restart with run.sh)' if rss > 400 else ''))
    A.check('Process', process)
    return A


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out')
    ap.add_argument('--no-network', action='store_true', help='skip the Kalshi comparisons')
    a = ap.parse_args(argv)
    result = run(network=not a.no_network)
    text = result.markdown()
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
    sys.stdout.write(text)
    return 1 if result.failed else 0


if __name__ == '__main__':
    sys.exit(main())
