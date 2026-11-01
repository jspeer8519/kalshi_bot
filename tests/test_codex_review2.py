"""Codex review (second round), findings 1-4: each test reproduces the reported failure."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import finalminute as fm
import indicators as ind
from executors import LiveSmallExecutor
from helpers import CLOSE, NOW, EngineCase, market

N = NOW


class QuoteAgeTests(unittest.TestCase):
    """1. Delay earlier in the same update must count toward the live quote-age check."""
    def test_delay_before_the_executor_counts(self):
        issues = []
        eng = SimpleNamespace(tick_wall=time.time() - 12, _issue=lambda *a, **k: issues.append(a),
                              log=SimpleNamespace(write=lambda *a, **k: None))
        m = dict(ticker='T', fetched=N, close=N + 300)
        self.assertTrue(LiveSmallExecutor(None)._stale(eng, 'BTC', m, N, time.time(), 'entry'))   # 12 s old: not sent
        eng.tick_wall = time.time()
        self.assertFalse(LiveSmallExecutor(None)._stale(eng, 'BTC', m, N, time.time(), 'entry'))


class MonitorOrderTests(EngineCase):
    def test_monitor_runs_after_the_decisions(self):
        seen = []
        self.e.final_minute = lambda asset, m, now: seen.append('BTC' in self.e.s['positions'])
        self.e.control('start'); self.e.tick(market('BTC'), NOW)
        self.assertEqual(seen, [True])                     # the entry was already made when the monitor ran


class ReversalCoverageTests(unittest.TestCase):
    """2. Missing index values must never confirm a reversal."""
    def test_two_samples_sixty_seconds_apart(self):
        self.assertIsNone(ind.reversal([(N - 60, 99.0), (N, 99.0)], 100, 'yes', N, .02, 60))

    def test_one_stale_sample(self):
        self.assertIsNone(ind.reversal([(N - 5, 99.0)], 100, 'yes', N, .02, 5))

    def test_gap_inside_the_window(self):
        ticks = [(t, 99.0) for t in range(N - 10, N + 1) if t not in (N - 3, N - 2)]
        self.assertIsNone(ind.reversal(ticks, 100, 'yes', N, .02, 5))

    def test_continuous_values_still_confirm(self):
        self.assertIsNotNone(ind.reversal([(t, 99.0) for t in range(N - 10, N + 1)], 100, 'yes', N, .02, 5))


def wiggle(start, end, level):
    return [(t, level + (.01 if t % 2 else -.01)) for t in range(start, end)]


class FinalProbCoverageTests(unittest.TestCase):
    """3. A missing past settlement second is not a future step."""
    def test_codex_case_last_thirty_only(self):
        close = N + 1
        ticks = wiggle(close - 1000, close - 60, 100.0) + [(t, 101.0) for t in range(close - 30, close)]
        self.assertIsNone(fm.final_prob(ticks, 100.0, close, close - 1))

    def test_gap_in_the_middle(self):
        ticks = [x for x in wiggle(CLOSE - 1000, CLOSE - 10, 100.0) if x[0] != CLOSE - 40]
        self.assertIsNone(fm.final_prob(ticks, 100.0, CLOSE, CLOSE - 11))

    def test_complete_so_far_counts_only_unpublished_seconds(self):
        ticks = wiggle(CLOSE - 1000, CLOSE - 1, 100.0)          # published through close-2
        r = fm.final_prob(ticks, 100.0, CLOSE, CLOSE - 1.5)
        self.assertEqual((r['known'], r['remaining']), (59, 1))


class OutcomeTests(EngineCase):
    """4. Fewer than 60 settlement seconds is provisional, never a result."""
    def test_codex_case_55_values(self):
        avg, n = fm.final_average([(t, 99.9) for t in range(N - 60, N - 5)], N)
        self.assertEqual(n, 55)
        self.cfg(final_minute_log=True)
        self.spot['BTC'] = wiggle(CLOSE - 1000, CLOSE - 60, 100.0) + [(t, 99.9) for t in range(CLOSE - 60, CLOSE - 5)]
        self.e.tick(market('BTC', strike=100.0, fetched=CLOSE - 30), CLOSE - 30)
        self.e.tick(market('BTC', ticker='KXBTC15M-T2', close=CLOSE + 900, strike=99.0, fetched=CLOSE + 400), CLOSE + 400)
        self.e.fm_flush()
        out = [json.loads(x) for x in Path(self.e.fm_path).read_text().splitlines() if '"outcome"' in x]
        self.assertIsNone(out[0]['result']); self.assertEqual(out[0]['values'], 55)
        self.assertAlmostEqual(out[0]['provisional_avg'], 99.9)

    def test_report_excludes_incomplete(self):
        import subprocess, sys
        rows = [dict(kind='snap', t=1, ticker='A', asset='BTC', close=2, strike=100, sec_left=5, model=dict(p_yes=.9), yes_ask=.5, yes_ask_size=5, no_ask=.5, no_ask_size=5),
                dict(kind='outcome', ticker='A', result=None, values=55)]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'fm.jsonl'; f.write_text('\n'.join(map(json.dumps, rows)) + '\n')
            out = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / 'finalminutereport.py'), '--file', str(f)], capture_output=True, text=True, timeout=60)
            self.assertIn('0 with a complete outcome', out.stdout); self.assertIn('1 excluded as incomplete', out.stdout)


if __name__ == '__main__':
    unittest.main()
