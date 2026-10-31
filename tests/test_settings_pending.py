"""Items 1-2: new defaults, and settings saved while positions are open become pending."""
import json
import sqlite3

from engine import DEFAULTS, Engine, validate
from helpers import NOW, EngineCase, market


class DefaultsTests(EngineCase):
    def test_defaults_dollars_and_assets(self):
        self.assertEqual(DEFAULTS['dollars'], 20)
        self.assertEqual(DEFAULTS['assets'], ['BTC', 'ETH', 'SOL', 'DOGE'])

    def test_new_flags_default_off_and_cap_defaults_to_one(self):
        for k in ('paper_whole_contracts', 'paper_depth_fill', 'direction_filter', 'fair_value', 'settlement_tracking',
                  'kill_switch', 'audit_full', 'fill_comparison'):
            self.assertIs(DEFAULTS[k], False, k)
        self.assertEqual((DEFAULTS['paper_latency_ms'], DEFAULTS['shadow_latency_ms']), (0, 0))
        self.assertEqual(DEFAULTS['max_same_side'], 1)
        self.assertEqual(DEFAULTS['kill_switch_pct'], .20)

    def test_old_config_gains_new_settings(self):
        old = {k: v for k, v in DEFAULTS.items() if k not in ('fair_value', 'max_same_side', 'kill_switch')}
        db = sqlite3.connect(self.path + '2'); db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(dict(config=old, cash=1000, positions={}, trades=[], events=[], seen=[], curve=[])),)); db.commit(); db.close()
        e = Engine(self.path + '2')
        self.assertEqual((e.s['config']['fair_value'], e.s['config']['max_same_side']), (False, 1))
        validate(json.loads(json.dumps(e.s['config']))); e.db.close()

    def test_flags_must_be_booleans(self):
        with self.assertRaises(ValueError): validate(dict(DEFAULTS, fair_value=1))
        for bad in (dict(paper_latency_ms=-1), dict(max_same_side=1.5), dict(edge_margin=.9), dict(kill_switch_pct=0), dict(vol_lookback_s=10)):
            with self.assertRaises(ValueError): validate(dict(DEFAULTS, **bad))


class PendingSettingsTests(EngineCase):
    def enter(self):
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertIn('BTC', self.e.s['positions'])

    def test_save_with_open_position_is_pending_then_applies_when_flat(self):
        self.enter()
        r = self.e.configure(dict(DEFAULTS, assets=['BTC', 'ETH', 'SOL', 'DOGE'], dollars=30))
        self.assertEqual(r, dict(pending=True, changed=['dollars']))
        self.assertEqual(self.e.s['config']['dollars'], 20)
        snap = self.e.snapshot()
        self.assertEqual(snap['pending_config']['dollars'], 30); self.assertEqual(snap['pending_changed'], ['dollars'])
        self.assertTrue(any('PENDING' in msg for msg in self.messages()))
        self.e.tick(market(status='finalized', result='yes', fetched=NOW+200), NOW+200)    # position settles -> flat
        self.assertNotIn('BTC', self.e.s['positions'])
        self.assertEqual(self.e.s['config']['dollars'], 30); self.assertIsNone(self.e.s['pending_config'])
        kinds = [ev['event'] for ev in self.events()]
        self.assertIn('config_pending', kinds); self.assertIn('config_applied', kinds)

    def test_invalid_settings_rejected_immediately_even_with_positions(self):
        self.enter()
        with self.assertRaises(ValueError): self.e.configure(dict(DEFAULTS, stop=.99))
        self.assertIsNone(self.e.s['pending_config'])

    def test_save_while_running_but_flat_applies_now(self):
        self.e.control('start')
        self.assertEqual(self.e.configure(dict(DEFAULTS, dollars=25))['pending'], False)
        self.assertEqual(self.e.s['config']['dollars'], 25)

    def test_cancel_pending(self):
        self.enter(); self.e.configure(dict(DEFAULTS, dollars=30))
        self.e.control('cancel_pending')
        self.assertIsNone(self.e.s['pending_config'])
        with self.assertRaises(ValueError): self.e.control('cancel_pending')

    def test_pending_survives_restart_and_applies_at_startup_when_flat(self):
        self.enter(); self.e.configure(dict(DEFAULTS, dollars=30))
        other = self.make(); self.assertEqual(other.s['config']['dollars'], 20); other.db.close()   # still open
        self.e.tick(market(status='finalized', result='yes', fetched=NOW+200), NOW+200)
        self.assertEqual(self.e.s['config']['dollars'], 30)

    def test_in_flight_simulated_entry_keeps_settings_pending(self):
        self.e.s['config']['paper_latency_ms'] = 1000
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertEqual(self.e.configure(dict(DEFAULTS, dollars=30))['pending'], True)


class SaveThrottleTests(EngineCase):
    """Routine refreshes save at most every SAVE_EVERY seconds; trades and positions save at once."""
    def saves(self):
        calls = []
        real = self.e.save
        self.e.save = lambda: (calls.append(1), real())
        return calls

    def test_quiet_refreshes_do_not_rewrite_state_every_tick(self):
        calls = self.saves()
        for i in range(10): self.e.tick(market(yes_ask=.5, fetched=NOW + i), NOW + i)    # nothing to do
        self.assertLessEqual(len(calls), 1)

    def test_entry_and_exit_are_saved_immediately(self):
        self.e.control('start'); calls = self.saves()
        self.e.tick(market(), NOW)                                              # entry
        self.assertEqual(len(calls), 1)
        self.assertIn('BTC', self.make().s['positions'])                         # on disk right away
        self.e.tick(market(yes_bid=.96, fetched=NOW + 1), NOW + 1)              # take profit
        self.assertEqual(len(calls), 2)
        self.assertNotIn('BTC', self.make().s['positions'])

    def test_routine_save_happens_after_the_interval(self):
        import engine
        calls = self.saves()
        self.e._saved_at -= engine.SAVE_EVERY + 1
        self.e.tick(market(yes_ask=.5), NOW)
        self.assertEqual(len(calls), 1)
