"""Items 10-11: append-only audit log, and the per-trade fill comparison report."""
import json
from pathlib import Path

import report
from audit import TradeLog, scrub, verify
from helpers import CLOSE, NOW, EngineCase, FakeExecutor, market


class AuditLogTests(EngineCase):
    def test_hash_chain_detects_edits_and_deletions(self):
        p = Path(self.temp.name) / 'a.jsonl'
        p.write_text('{"ts": "old", "event": "legacy"}\n')                     # lines from before the chain
        log = TradeLog(str(p))
        for i in range(3): log.write('x', i=i)
        TradeLog(str(p)).write('x', i=3)                                       # chain continues after a restart
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        self.assertEqual([r.get('seq') for r in rows], [None, 1, 2, 3, 4])
        self.assertEqual(verify(str(p)), (True, None))
        lines = p.read_text().splitlines()
        p.write_text('\n'.join(lines[:2] + [lines[2].replace('"i": 1', '"i": 9')] + lines[3:]) + '\n')
        self.assertFalse(verify(str(p))[0])
        p.write_text('\n'.join(lines[:2] + lines[3:]) + '\n')
        self.assertFalse(verify(str(p))[0])

    def test_secrets_are_scrubbed(self):
        pem = '-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'
        self.assertEqual(scrub(dict(pem=pem, key_id='k1', nested=[dict(signature='s', ok=1), 'x ' + pem], headers={'KALSHI-ACCESS-KEY': 'k'})),
                         dict(pem='[REDACTED]', key_id='[REDACTED]', nested=[dict(signature='[REDACTED]', ok=1), '[REDACTED]'],
                              headers={'KALSHI-ACCESS-KEY': '[REDACTED]'}))
        self.e.log.write('test', creds=dict(key_id='KEYID-not-hex-xyz', pem=pem))     # never occurs in hex hashes by chance
        body = Path(self.logp).read_text()
        self.assertNotIn('PRIVATE KEY', body); self.assertNotIn('KEYID-not-hex-xyz', body)

    def test_full_audit_logs_decisions_with_inputs(self):
        self.cfg(audit_full=True)
        self.e.control('start'); self.e.tick(market(), NOW)
        for i in range(3): self.e.tick(market('ETH', fetched=NOW+i), NOW+i)    # same-side cap: one skip line, not three
        ev = self.events()
        entry = next(x for x in ev if x['event'] == 'decision_entry')
        self.assertEqual(entry['inputs']['quotes']['yes']['ask'], .85); self.assertEqual(entry['order']['count'], 1)
        self.assertEqual(len([x for x in ev if x['event'] == 'decision_skip' and x['asset'] == 'ETH']), 1)
        self.assertEqual(verify(self.logp), (True, None))

    def test_decisions_not_logged_by_default_but_orders_fills_and_config_are(self):
        self.e.control('start'); self.e.tick(market(), NOW)
        self.e.configure(dict(self.e.s['config'], dollars=30))
        kinds = [x['event'] for x in self.events()]
        self.assertNotIn('decision_entry', kinds)
        for k in ('startup', 'entries_enabled', 'paper_fill', 'config_pending'): self.assertIn(k, kinds)


class ComparisonReportTests(EngineCase):
    def test_paper_mode_compares_paper_with_shadow(self):
        self.cfg(fill_comparison=True, contracts=3)
        self.e.control('start')
        self.e.tick(market(no_bids=[[.15, 2.5], [.14, 10]]), NOW)               # paper (legacy): 2.5 @85c; shadow: 2 @85c
        self.e.tick(market(yes_bid=.96, yes_bids=[[.96, 1], [.95, 10]], fetched=NOW+1), NOW+1)
        row = self.e.comparison_report()[0]
        self.assertEqual((row['paper_entry'], row['shadow_entry'], row['live_entry']), (.85, .85, None))
        self.assertEqual(row['entry_ref'], .85); self.assertEqual(row['paper_entry_slip'], 0)
        self.assertEqual(row['exit_ref'], .96)
        self.assertAlmostEqual(row['paper_exit'], (.96 + 1.5 * .95) / 2.5)
        self.assertAlmostEqual(row['shadow_exit'], (.96 + .95) / 2)
        self.assertAlmostEqual(row['paper_exit_slip'], .96 - row['paper_exit'])
        self.assertIsNotNone(row['shadow_vs_paper']); self.assertIsNone(row['live_vs_paper'])

    def test_live_small_records_live_fill_and_simulated_fills_for_one_contract(self):
        x = FakeExecutor(); x.open_result = dict(size=1, price=.86, fee=.01, order_id='o1', client_order_id='c')
        e = self.e.__class__(self.path + 'L', executor=x, log_path=self.logp, live_enabled=True); self.addCleanup(e.db.close)
        e.s['config'].update(fill_comparison=True, contracts=5); e.set_mode('live'); e.control('start')
        e.tick(market(), NOW)
        e.tick(market(status='finalized', result='yes', fetched=CLOSE+5), CLOSE+5)
        row = e.comparison_report()[0]
        self.assertEqual((row['live_entry'], row['paper_entry'], row['shadow_entry']), (.86, .85, .85))
        self.assertAlmostEqual(row['live_entry_slip'], .01)                      # paid 1c above the quote
        self.assertEqual((row['live_exit'], row['paper_exit']), (1.0, 1.0))      # settlement pays every model the same
        self.assertAlmostEqual(row['live_vs_paper'], -.01)
        rec = next(iter(e.s['fills'].values())); self.assertEqual(rec['count'], 1)

    def test_simulated_latency_fills_arrive_later(self):
        self.cfg(fill_comparison=True, shadow_latency_ms=1000)
        self.e.control('start'); self.e.tick(market(), NOW)
        rec = next(iter(self.e.s['fills'].values())); self.assertNotIn('shadow', rec)
        self.e.tick(market(no_bids=[[.16, 10]], fetched=NOW+1), NOW+1)
        self.assertEqual(rec['shadow']['price'], .84)
        self.assertIn('BTC', self.e.s['positions']); self.assertEqual(self.e.s['positions']['BTC']['mode'], 'paper')

    def test_off_by_default_and_csv(self):
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertEqual(self.e.s['fills'], {}); self.assertEqual(self.e.comparison_report(), [])
        self.assertTrue(report.to_csv([]).startswith('closed,asset,ticker,side,mode,reason,contracts,entry_ref'))
