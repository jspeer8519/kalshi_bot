"""Final-minute monitor: probability from the known part of the settlement average, record-only logging, report."""
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import finalminute as fm
from helpers import CLOSE, NOW, EngineCase, market
from settings import DEFAULTS, validate

ROOT = Path(__file__).resolve().parent.parent


def ticks(level, until=NOW, start=CLOSE - 1000):
    return [(t, level + .05 * math.sin(t / 3)) for t in range(start, until + 1)]


class ProbabilityTests(unittest.TestCase):
    def test_well_above_with_seconds_left_is_near_certain(self):
        now = CLOSE - 5
        p = fm.final_prob(ticks(101.0, now), 100.0, CLOSE, now)
        self.assertGreater(p['p_yes'], .999); self.assertEqual(p['remaining'], 4)       # values at close-4 .. close-1 are still to come

    def test_right_at_the_target_is_a_coin_flip(self):
        now = CLOSE - 30
        p = fm.final_prob(ticks(100.0, now), 100.0, CLOSE, now)['p_yes']
        self.assertTrue(.2 < p < .8, p)

    def test_only_inside_the_final_minute_and_no_look_ahead(self):
        self.assertIsNone(fm.final_prob(ticks(101.0, CLOSE - 70), 100.0, CLOSE, CLOSE - 70))
        now = CLOSE - 20
        future = ticks(101.0, now) + [(now + s, 50.0) for s in range(1, 20)]
        self.assertGreater(fm.final_prob(future, 100.0, CLOSE, now)['p_yes'], .99)

    def test_outcome_from_recorded_values(self):
        avg, n = fm.final_average(ticks(100.2, CLOSE + 5), CLOSE)
        self.assertEqual(n, 60); self.assertAlmostEqual(avg, 100.2, places=1)

    def test_flag_off_by_default(self):
        self.assertFalse(DEFAULTS['final_minute_log']); validate(dict(DEFAULTS, assets=list(DEFAULTS['assets'])))


class EngineTests(EngineCase):
    def rows(self):
        p = Path(self.e.fm_path)
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_off_writes_nothing_and_on_records_only_the_final_minute(self):
        self.spot['BTC'] = ticks(101.0, CLOSE + 10)
        self.e.tick(market('BTC', strike=100.0, fetched=CLOSE - 30), CLOSE - 30)
        self.assertEqual(self.rows(), [])
        self.cfg(final_minute_log=True)
        self.e.tick(market('BTC', strike=100.0, fetched=CLOSE - 90), CLOSE - 90)          # before the final minute
        self.e.tick(market('BTC', strike=100.0, fetched=CLOSE - 20), CLOSE - 20)
        snaps = [r for r in self.rows() if r['kind'] == 'snap']
        self.assertEqual(len(snaps), 1)
        self.assertGreater(snaps[0]['model']['p_yes'], .99)
        self.assertEqual(self.e.s['positions'], {})                                  # recording never trades (engine not started)

    def test_outcome_is_recorded_after_close(self):
        self.cfg(final_minute_log=True); self.spot['BTC'] = ticks(99.0, CLOSE + 10)
        self.e.tick(market('BTC', strike=100.0, fetched=CLOSE - 10), CLOSE - 10)
        self.e.tick(market('BTC', ticker='KXBTC15M-T2', close=CLOSE + 900, strike=99.0, fetched=CLOSE + 5), CLOSE + 5)
        out = [r for r in self.rows() if r['kind'] == 'outcome']
        self.assertEqual((out[0]['ticker'], out[0]['result']), ('KXBTC15M-T1', 'no'))


class ReportTests(unittest.TestCase):
    def test_report_runs(self):
        rows = []
        for i in range(6):
            tk = f'KXBTC15M-{i}'; won = i < 5
            rows.append(dict(kind='snap', t=1, ticker=tk, asset='BTC', close=2, strike=100, sec_left=8, model=dict(p_yes=.97),
                             yes_ask=.60, yes_ask_size=50, no_ask=.42, no_ask_size=50))
            rows.append(dict(kind='outcome', ticker=tk, result='yes' if won else 'no'))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'fm.jsonl'; f.write_text('\n'.join(map(json.dumps, rows)) + '\n')
            out = subprocess.run([sys.executable, str(ROOT / 'finalminutereport.py'), '--file', str(f)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('edge >= 10c: 6 markets, won 83.3%', out.stdout)
            self.assertIn('CLOSE RACES', out.stdout)


if __name__ == '__main__':
    unittest.main()
