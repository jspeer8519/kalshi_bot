"""Items 7-8: settlement tracking for early exits, and the equity kill-switch."""
from helpers import CLOSE, NOW, EngineCase, market


class SettlementTrackingTests(EngineCase):
    def stop_out(self):
        self.cfg(stop_seconds=0)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.e.tick(market(yes_bid=.30, fetched=NOW+1), NOW+1)
        self.assertEqual(self.e.s['trades'][0]['reason'], 'stop loss')

    def test_off_by_default(self):
        self.stop_out()
        self.assertEqual(self.e.s['settle_watch'], []); self.assertNotIn('settled_result', self.e.s['trades'][0])

    def test_records_how_a_stopped_market_settled(self):
        self.cfg(settlement_tracking=True); self.stop_out()
        self.assertEqual(self.e.settlement_due(NOW+60), [])                       # not closed yet
        self.assertEqual(self.e.settlement_due(CLOSE+1), ['KXBTC15M-T1'])
        self.e.record_settlement('KXBTC15M-T1', 'closed', None, CLOSE+1)         # not final yet: keep watching
        self.assertEqual(self.e.settlement_due(CLOSE+2), [])                      # re-check interval
        self.e.record_settlement('KXBTC15M-T1', 'finalized', 'yes', CLOSE+40)
        t = self.e.s['trades'][0]
        self.assertEqual((t['settled_result'], t['hold_value']), ('yes', 1.0))
        self.assertAlmostEqual(t['hold_pnl'], 1 - .86); self.assertAlmostEqual(t['exit_vs_hold'], t['pnl'] - (1 - .86))
        self.assertEqual(self.e.s['settle_watch'], [])
        self.assertTrue(any('versus holding' in m for m in self.messages()))
        self.assertIn('settlement_recorded', [ev['event'] for ev in self.events()])

    def test_take_profit_is_tracked_and_settlement_close_is_not(self):
        self.cfg(settlement_tracking=True)
        self.e.control('start'); self.e.tick(market(), NOW); self.e.tick(market(yes_bid=.96, fetched=NOW+1), NOW+1)
        self.assertEqual(len(self.e.s['settle_watch']), 1)
        self.e.tick(market('ETH'), NOW); self.cfg(max_same_side=0); self.e.tick(market('ETH', fetched=NOW+2), NOW+2)
        self.e.tick(market('ETH', status='finalized', result='no', fetched=CLOSE+5), CLOSE+5)
        self.assertEqual(len(self.e.s['settle_watch']), 1)                         # held to settlement: nothing to track

    def test_gives_up_after_a_day(self):
        self.cfg(settlement_tracking=True); self.stop_out()
        self.e.record_settlement('KXBTC15M-T1', 'closed', None, CLOSE + 86401)
        self.assertEqual(self.e.s['settle_watch'], [])


class KillSwitchTests(EngineCase):
    def test_off_by_default(self):
        self.e.s['cash'] = 500; self.e.control('start'); self.e.tick(market(), NOW)
        self.assertTrue(self.e.running); self.assertEqual(self.e.s['kill'], {})

    def test_halts_entries_at_drawdown_from_peak(self):
        self.cfg(kill_switch=True, kill_switch_pct=.2)
        self.e.control('start'); self.e.tick(market('ETH', yes_ask=.5), NOW)     # peak recorded at $1,000
        self.e.s['cash'] = 801; self.e.tick(market('ETH', yes_ask=.5, fetched=NOW+1), NOW+1)
        self.assertTrue(self.e.running)
        self.e.s['cash'] = 800; self.e.tick(market(fetched=NOW+2), NOW+2)
        self.assertFalse(self.e.running); self.assertNotIn('BTC', self.e.s['positions'])
        self.assertIn('20.0% below peak', self.e.s['kill']['paper']['reason'])
        with self.assertRaises(ValueError): self.e.control('start')
        other = self.make(); self.assertIn('paper', other.s['kill']); other.db.close()   # survives restart
        self.e.control('reset_kill')
        self.assertEqual(self.e.s['equity_peak']['paper'], 800)
        self.e.control('start'); self.e.tick(market(fetched=NOW+3), NOW+3)
        self.assertIn('BTC', self.e.s['positions'])

    def test_open_positions_are_marked_at_bid(self):
        self.cfg(kill_switch=True, size_mode='dollars', dollars=300, daily_loss=0)   # equity ~$703 once marked at 1c
        self.e.control('start'); self.e.tick(market(yes_ask_size=1000), NOW)
        self.e.tick(market(yes_bid=.01, yes_bid_size=1000, fetched=NOW+1), NOW+1)   # stop .40 needs 1s confirmation
        self.assertIn('paper', self.e.s['kill'])
        self.assertIn('BTC', self.e.s['positions'])                                 # exits still managed

    def test_equity_is_per_mode(self):
        self.e.s['trades'].insert(0, dict(mode='shadow', pnl=-50, closed=NOW, ticker='X', side='yes', size=1))
        self.assertEqual(self.e.equity('shadow'), 950); self.assertEqual(self.e.equity('paper'), 1000)
        self.assertIsNone(self.e.equity('live'))
        self.e.s['live_balance'] = 40; self.assertEqual(self.e.equity('live'), 40)
