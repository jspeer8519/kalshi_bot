"""Daily audit script: results, exit status, failures isolated per check. No network, no running app."""
import unittest
from unittest.mock import patch

import auditcheck

STATE = dict(trades=[], positions={}, shadow_positions={}, cash=1000, curve=[])
APP_STATE = dict(mode='PAPER', running=True, config=None, now=1000.0, errors={}, pending_changed=[],
                 markets=dict(BTC=dict(fetched=999.0, status='active', strike=77000.0)))


def fake_get(url, timeout=10):
    if url.endswith('/api/state'):
        return APP_STATE
    if '/api/index' in url:
        return dict(status='connected', error=None, index=dict(BTC=dict(avg=[[1, 2]])))
    raise OSError('offline in tests')


class AuditTests(unittest.TestCase):
    def run_audit(self, state=STATE, app_state=None):
        from settings import DEFAULTS
        st = dict(APP_STATE, config=dict(DEFAULTS), **(app_state or {}))
        with patch.object(auditcheck.pnlreport, 'load_state', return_value=state), \
             patch.object(auditcheck, 'get', side_effect=lambda u, timeout=10: st if u.endswith('/api/state') else fake_get(u, timeout)):
            return auditcheck.run(network=False)

    def test_healthy_state_passes_core_checks(self):
        a = self.run_audit()
        rows = {c: s for s, c, _ in a.rows}
        for c in ('App and configuration', 'Market feeds', 'Settlement index stream', 'Paper cash reconciliation', 'Trade records'):
            self.assertEqual(rows[c], 'PASS', c)

    def test_cash_mismatch_and_stale_feed_fail(self):
        a = self.run_audit(state=dict(STATE, cash=900), app_state=dict(now=1100.0))
        rows = {c: s for s, c, _ in a.rows}
        self.assertEqual(rows['Paper cash reconciliation'], 'FAIL'); self.assertEqual(rows['Market feeds'], 'FAIL')
        self.assertTrue(a.failed)

    def test_paused_engine_is_a_warning(self):
        a = self.run_audit(app_state=dict(running=False))
        self.assertIn(('WARN', 'Entries', 'Engine is paused: press Start to trade'), a.rows)

    def test_a_broken_check_does_not_stop_the_others(self):
        with patch.object(auditcheck.pnlreport, 'load_state', return_value=STATE), patch.object(auditcheck, 'get', side_effect=OSError('down')):
            a = auditcheck.run(network=False)
        self.assertEqual(a.rows[0][0], 'FAIL'); self.assertGreater(len(a.rows), 3)

    def test_markdown_summary_and_exit_status(self):
        a = auditcheck.Audit(); a.add('PASS', 'x', 'ok'); a.add('WARN', 'y', 'hmm')
        self.assertIn('**1 passed · 1 warnings · 0 failed**', a.markdown()); self.assertFalse(a.failed)
        a.add('FAIL', 'z', 'bad'); self.assertTrue(a.failed)


class ChartPointTests(unittest.TestCase):
    def test_prefers_the_exact_second_then_the_nearest(self):
        self.assertEqual(auditcheck.chart_point({100: 1.0, 101: 2.0}, 100), (1.0, 0))
        self.assertEqual(auditcheck.chart_point({101: 2.0, 98: 3.0}, 100), (2.0, 1))      # stamped a second late
        self.assertEqual(auditcheck.chart_point({102: 4.0}, 100), (4.0, 2))
        self.assertEqual(auditcheck.chart_point({103: 5.0, 96: 6.0}, 100), (None, None))
