"""Live-mode tests. Every Kalshi call is faked; nothing here touches the network."""
import json
import tempfile
import unittest
from pathlib import Path

from engine import Engine
from live import LiveAmbiguous, LiveExecutor, OrderRejected


class FakeExecutor:
    def __init__(self):
        self.held = 0.0
        self.cash = 50.0
        self.open_result = None
        self.open_error = None
        self.close_result = None
        self.close_error = None
        self.calls = []

    def position(self, ticker):
        self.calls.append(('position', ticker)); return self.held

    def balance(self):
        return self.cash

    def open(self, ticker, side, price, count):
        self.calls.append(('open', ticker, side, price, count))
        if self.open_error: raise self.open_error
        return self.open_result

    def close(self, ticker, side, bid, count):
        self.calls.append(('close', ticker, side, bid, count))
        if self.close_error: raise self.close_error
        return self.close_result


class EngineLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'paper.db')
        self.logp = str(Path(self.temp.name) / 'trade_log.jsonl')
        self.x = FakeExecutor()
        self.e = Engine(self.db, live_enabled=True, executor=self.x, log_path=self.logp)
        self.now = 1800000000

    def tearDown(self):
        self.e.db.close(); self.temp.cleanup()

    def market(self, **kw):
        return dict(dict(asset='BTC',ticker='TEST-1', status='active', result='', close=self.now+120, fetched=self.now,
                         yes_ask=.85, no_ask=.17, yes_bid=.83, no_bid=.15, yes_ask_size=10, no_ask_size=10,
                         yes_bid_size=10, no_bid_size=10), **kw)

    def go_live(self):
        self.e.set_mode('live'); self.e.control('start')

    def log_events(self):
        return [json.loads(line)['event'] for line in Path(self.logp).read_text().splitlines()]

    def test_restart_always_paper(self):
        self.e.set_mode('live')
        other = Engine(self.db, live_enabled=True, executor=self.x); self.assertEqual(other.mode, 'paper'); other.db.close()

    def test_mode_switch_rules(self):
        self.e.control('start')
        with self.assertRaises(ValueError): self.e.set_mode('live')          # must pause first
        self.e.control('pause')
        self.e.s['config']['contracts'] = 2
        self.e.live_max_contracts = 0                                        # LIVE_SMALL still sends 1: above a zero cap
        with self.assertRaises(ValueError): self.e.set_mode('live')
        self.e.live_max_contracts = 1; self.e.s['config']['contracts'] = 1
        no_exec = Engine(str(Path(self.temp.name) / 'b.db'))
        with self.assertRaises(ValueError): no_exec.set_mode('live')         # no executor
        no_exec.db.close()

    def test_live_small_sends_one_contract_for_dollar_sizing(self):
        self.e.s['config'].update(size_mode='dollars', dollars=10)          # decision: 11 contracts at 85c
        self.e.set_mode('live')                                              # LIVE_SMALL: allowed, sends 1
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='o', client_order_id='c')
        self.e.control('start'); self.e.tick(self.market(yes_ask_size=20), self.now)
        self.assertEqual(self.x.calls[1], ('open', 'TEST-1', 'yes', .85, 1))
        self.assertEqual(self.e.s['positions']['BTC']['size'], 1)

    def test_no_switch_with_open_position(self):
        self.e.control('start'); self.e.tick(self.market(), self.now); self.e.control('pause')
        with self.assertRaises(ValueError): self.e.set_mode('live')

    def test_live_entry_uses_kalshi_fill_and_leaves_paper_cash(self):
        self.x.open_result = dict(size=1, price=.84, fee=.01, order_id='ord-1', client_order_id='c1')
        self.go_live(); self.e.tick(self.market(), self.now)
        p = self.e.s['positions'].get('BTC')
        self.assertEqual((p['mode'], p['entry'], p['order_id']), ('live', .84, 'ord-1'))
        self.assertAlmostEqual(p['cost'], .85)
        self.assertEqual(self.e.s['cash'], 1000)
        self.assertEqual(self.x.calls[1], ('open', 'TEST-1', 'yes', .85, 1))
        self.assertIn('order_submit', self.log_events()); self.assertIn('order_result', self.log_events())

    def test_ambiguous_entry_halts_and_never_retries(self):
        self.x.open_error = LiveAmbiguous('timeout')
        self.go_live(); self.e.tick(self.market(), self.now)
        self.assertIsNone(self.e.s['positions'].get('BTC')); self.assertFalse(self.e.running)
        self.assertEqual(self.e.s['live_halt'], 'timeout'); self.assertIn('TEST-1', self.e.s['seen'])
        with self.assertRaises(ValueError): self.e.control('start')
        self.e.control('clear_halt'); self.assertIsNone(self.e.s['live_halt'])
        other = Engine(self.db, live_enabled=True, executor=self.x)   # halt cleared state persists too
        self.assertIsNone(other.s['live_halt']); other.db.close()

    def test_halt_survives_restart(self):
        self.x.open_error = LiveAmbiguous('timeout')
        self.go_live(); self.e.tick(self.market(), self.now)
        other = Engine(self.db, live_enabled=True, executor=self.x); self.assertEqual(other.s['live_halt'], 'timeout'); other.db.close()

    def test_rejected_entry_skips_market_without_halt(self):
        self.x.open_error = OrderRejected('HTTP 400 insufficient_balance')
        self.go_live(); self.e.tick(self.market(), self.now); self.e.tick(self.market(fetched=self.now+2), self.now+2)
        self.assertIsNone(self.e.s['live_halt']); self.assertIsNone(self.e.s['positions'].get('BTC'))
        self.assertEqual(len([c for c in self.x.calls if c[0] == 'open']), 1)

    def test_existing_account_position_blocks_entry(self):
        self.x.held = 3
        self.go_live(); self.e.tick(self.market(), self.now)
        self.assertFalse([c for c in self.x.calls if c[0] == 'open'])

    def test_insufficient_balance_blocks_entry(self):
        self.x.cash = .5
        self.go_live(); self.e.tick(self.market(), self.now)
        self.assertFalse([c for c in self.x.calls if c[0] == 'open'])

    def test_live_stop_partial_then_full_exit(self):
        # A 2-contract live position (e.g. restored from an earlier version) still exits correctly.
        self.go_live()
        self.e.s['positions']['BTC'] = dict(ticker='TEST-1', side='yes', entry=.85, size=2, entry_fee=.02, cost=1.72, opened=self.now, mark=.83, mode='live', order_id='ord-1')
        self.x.close_result = dict(size=1, price=.30, fee=.01, order_id='ord-2', client_order_id='c2')
        self.e.tick(self.market(yes_bid=.30), self.now); self.e.tick(self.market(yes_bid=.30, fetched=self.now+1), self.now+1)
        self.assertEqual(self.e.s['positions'].get('BTC')['size'], 1); self.assertEqual(self.e.s['trades'][0]['mode'], 'live')
        self.assertAlmostEqual(self.e.s['trades'][0]['pnl'], .30 - .01 - .86)
        self.e.tick(self.market(yes_bid=.30, fetched=self.now+2), self.now+2)
        self.assertIsNone(self.e.s['positions'].get('BTC')); self.assertEqual(self.e.s['cash'], 1000)

    def test_ambiguous_exit_halts_and_keeps_position(self):
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='ord-1', client_order_id='c1')
        self.go_live(); self.e.tick(self.market(), self.now)
        self.x.close_error = LiveAmbiguous('HTTP 503')
        self.e.tick(self.market(yes_bid=.96), self.now)
        self.assertIsNotNone(self.e.s['positions'].get('BTC')); self.assertEqual(self.e.s['live_halt'], 'HTTP 503')
        self.x.close_error = None; self.x.calls.clear()
        self.e.tick(self.market(yes_bid=.96, fetched=self.now+1), self.now+1)
        self.assertFalse([c for c in self.x.calls if c[0] == 'close'])        # nothing sent while halted

    def test_live_settlement_and_daily_pnl_by_mode(self):
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='ord-1', client_order_id='c1')
        self.go_live(); self.e.tick(self.market(), self.now)
        self.e.tick(self.market(status='finalized', result='yes'), self.now)
        self.assertAlmostEqual(self.e.daily_pnl(self.now, 'live'), .14)
        self.assertEqual(self.e.daily_pnl(self.now, 'paper'), 0)

    def test_restored_live_position_still_managed_in_paper_mode(self):
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='ord-1', client_order_id='c1')
        self.go_live(); self.e.tick(self.market(), self.now)
        other = Engine(self.db, live_enabled=True, executor=self.x)
        self.assertEqual(other.mode, 'paper')
        other.s['live_halt'] = None
        self.x.close_result = dict(size=1, price=.96, fee=.01, order_id='ord-2', client_order_id='c2')
        other.tick(self.market(yes_bid=.96), self.now)
        self.assertIsNone(other.s['positions'].get('BTC')); self.assertEqual(other.s['trades'][0]['exit_order_id'], 'ord-2')
        other.db.close()

    def test_paper_trades_are_logged(self):
        self.e.control('start'); self.e.tick(self.market(), self.now)
        self.e.tick(self.market(yes_bid=.96, fetched=self.now+2), self.now+2)
        events = self.log_events(); self.assertIn('paper_fill', events); self.assertIn('trade_closed', events)
        self.assertNotIn('PRIVATE', Path(self.logp).read_text())


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses); self.sent = []

    def request(self, method, path, body=None):
        self.sent.append((method, path, body))
        r = self.responses.pop(0)
        if isinstance(r, Exception): raise r
        return r


class ExecutorTests(unittest.TestCase):
    def ex(self, *responses):
        self.client = FakeClient(responses)
        return LiveExecutor(self.client, verify_delay=0, sleep=lambda s: None)

    def test_buy_yes_payload_and_fill(self):
        e = self.ex((201, dict(order_id='o1', fill_count='1.00', remaining_count='0.00', average_fill_price='0.8500', average_fee_paid='0.0100')),
                    (200, dict(market_positions=[dict(ticker='T', position_fp='1.00')])))
        fill = e.open('T', 'yes', .85, 1)
        _, path, body = self.client.sent[0]
        self.assertEqual(path, '/portfolio/events/orders')
        self.assertEqual((body['side'], body['price'], body['count'], body['time_in_force'], body['reduce_only']), ('bid', '0.8500', '1.00', 'immediate_or_cancel', False))
        self.assertEqual((fill['size'], fill['price'], fill['fee'], fill['order_id']), (1.0, .85, .01, 'o1'))

    def test_buy_no_maps_to_yes_ask(self):
        e = self.ex((201, dict(order_id='o1', fill_count='1.00', remaining_count='0', average_fill_price='0.1500', average_fee_paid='0.0100')),
                    (200, dict(market_positions=[dict(ticker='T', position_fp='-1.00')])))
        fill = e.open('T', 'no', .85, 1)
        self.assertEqual((self.client.sent[0][2]['side'], self.client.sent[0][2]['price']), ('ask', '0.1500'))
        self.assertAlmostEqual(fill['price'], .85)

    def test_sell_is_reduce_only(self):
        e = self.ex((201, dict(order_id='o2', fill_count='1.00', remaining_count='0', average_fill_price='0.3000', average_fee_paid='0.0100')))
        e.close('T', 'no', .70, 1)
        body = self.client.sent[0][2]
        self.assertEqual((body['side'], body['price'], body['reduce_only']), ('bid', '0.3000', True))

    def test_no_fill_returns_none(self):
        self.assertIsNone(self.ex((201, dict(order_id='o1', fill_count='0.00', remaining_count='0.00'))).open('T', 'yes', .85, 1))

    def test_outcomes(self):
        with self.assertRaises(OrderRejected): self.ex((400, dict(error=dict(code='invalid', message='bad')))).open('T', 'yes', .85, 1)
        with self.assertRaises(LiveAmbiguous): self.ex((503, {})).open('T', 'yes', .85, 1)
        with self.assertRaises(LiveAmbiguous): self.ex(TimeoutError()).open('T', 'yes', .85, 1)
        with self.assertRaises(LiveAmbiguous): self.ex((201, dict(fill_count='1.00'))).open('T', 'yes', .85, 1)

    def test_position_mismatch_halts_after_retries(self):
        mismatch = (200, dict(market_positions=[dict(ticker='T', position_fp='0')]))
        e = self.ex((201, dict(order_id='o1', fill_count='1.00', remaining_count='0', average_fill_price='0.85', average_fee_paid='0.01')), mismatch, mismatch, mismatch)
        with self.assertRaises(LiveAmbiguous): e.open('T', 'yes', .85, 1)

    def test_position_and_balance_reads(self):
        self.assertEqual(self.ex((200, dict(market_positions=[]))).position('T'), 0)
        self.assertIsNone(self.ex((500, {})).position('T'))
        self.assertEqual(self.ex((200, dict(balance_dollars='12.3456'))).balance(), 12.3456)
        self.assertEqual(self.ex((200, dict(balance=1234))).balance(), 12.34)


if __name__ == '__main__':
    unittest.main()


class LiveFillPersistenceTests(unittest.TestCase):
    """Safety review F1: a real fill is on disk before the follow-up balance request, so a crash or
    hang during that request can never lose a live position or a completed live sale."""
    def setUp(self):
        import sqlite3
        self.temp = tempfile.TemporaryDirectory(); self.db = str(Path(self.temp.name) / 'p.db'); self.now = 1800000000
        self.x = FakeExecutor(); self.on_disk = []
        def balance():
            body = json.loads(sqlite3.connect(self.db).execute('SELECT body FROM state').fetchone()[0])
            self.on_disk.append({a: p['size'] for a, p in body['positions'].items()})
            return self.x.cash
        self.x.balance = balance
        self.e = Engine(self.db, live_enabled=True, executor=self.x)

    def tearDown(self):
        self.e.db.close(); self.temp.cleanup()

    def market(self, **kw):
        return dict(dict(asset='BTC', ticker='TEST-1', status='active', result='', close=self.now+120, fetched=self.now, yes_ask=.85, no_ask=.17,
                         yes_bid=.83, no_bid=.15, yes_ask_size=10, no_ask_size=10, yes_bid_size=10, no_bid_size=10), **kw)

    def test_entry_and_exit_are_persisted_before_the_balance_request(self):
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='o1', client_order_id='c1')
        self.e.set_mode('live'); self.e.control('start'); self.e.tick(self.market(), self.now)
        self.assertEqual(self.on_disk[-1], {'BTC': 1})                       # read during the post-fill balance call
        self.x.close_result = dict(size=1, price=.96, fee=.01, order_id='o2', client_order_id='c2')
        self.e.tick(self.market(yes_bid=.96, fetched=self.now+1), self.now+1)
        self.assertEqual(self.on_disk[-1], {})


class CodexReviewTests(unittest.TestCase):
    """Second-opinion review (Codex) findings C1-C4: the live path fails closed."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.db = str(Path(self.temp.name) / 'p.db'); self.now = 1800000000
        self.logp = str(Path(self.temp.name) / 'log.jsonl')
        self.x = FakeExecutor(); self.e = Engine(self.db, live_enabled=True, executor=self.x, log_path=self.logp)

    def tearDown(self):
        self.e.db.close(); self.temp.cleanup()

    def market(self, **kw):
        return dict(dict(asset='BTC', ticker='TEST-1', status='active', result='', close=self.now+120, fetched=self.now, yes_ask=.85, no_ask=.17,
                         yes_bid=.83, no_bid=.15, yes_ask_size=10, no_ask_size=10, yes_bid_size=10, no_bid_size=10), **kw)

    def on_disk(self):
        import sqlite3
        return json.loads(sqlite3.connect(self.db).execute('SELECT body FROM state').fetchone()[0])

    # C1: an in-flight marker is on disk while the order is out, and a restart with it halts live trading
    def test_inflight_marker_is_saved_before_sending_and_cleared_with_the_fill(self):
        seen = []
        def open_(ticker, side, price, count):
            seen.append(self.on_disk()['live_inflight']); return dict(size=1, price=.85, fee=.01, order_id='o1', client_order_id='c')
        self.x.open = open_
        self.e.set_mode('live'); self.e.control('start'); self.e.tick(self.market(), self.now)
        self.assertEqual((seen[0]['ticker'], seen[0]['intent']), ('TEST-1', 'entry'))
        disk = self.on_disk(); self.assertIsNone(disk['live_inflight']); self.assertIn('BTC', disk['positions'])

    def test_restart_with_an_unresolved_order_halts_live_trading(self):
        self.e.s['live_inflight'] = dict(asset='BTC', ticker='TEST-1', side='yes', intent='entry', decision='d', at=1.0); self.e.save()
        other = Engine(self.db, live_enabled=True, executor=self.x)
        self.assertIn('unresolved', other.s['live_halt']); self.assertIsNone(other.s['live_inflight'])
        with self.assertRaises(ValueError): other.set_mode('live')
        other.db.close()

    def test_rejected_and_ambiguous_orders_clear_the_marker(self):
        self.x.open_error = OrderRejected('HTTP 400'); self.e.set_mode('live'); self.e.control('start'); self.e.tick(self.market(), self.now)
        self.assertIsNone(self.on_disk()['live_inflight'])
        self.x.open_error = LiveAmbiguous('timeout'); self.e.tick(self.market(ticker='TEST-2'), self.now)
        disk = self.on_disk(); self.assertIsNone(disk['live_inflight']); self.assertEqual(disk['live_halt'], 'timeout')

    # C2: a failing audit log never discards a fill, and it stops new live entries
    def test_log_failure_does_not_raise_and_blocks_live_entries(self):
        self.e.log.path = str(Path(self.temp.name) / 'missing-dir' / 'log.jsonl')
        self.e.log.write('x', a=1)
        self.assertEqual(self.e.log.failures, 1)
        self.e.set_mode('live'); self.e.control('start'); self.e.tick(self.market(), self.now)
        self.assertFalse([c for c in self.x.calls if c[0] == 'open'])
        self.assertIn('audit log', self.e.s['live_halt'])

    # C3: freshness is re-checked after the (slow) account checks, right before sending
    def test_quote_that_went_stale_during_account_checks_is_not_sent(self):
        import executors
        from unittest.mock import patch
        self.e.set_mode('live'); self.e.control('start')
        with patch.object(executors.time, 'time', side_effect=[0.0] + [15.0] * 40):  # decision at 0 s, sending at 15 s
            self.e.tick(self.market(), self.now)
        self.assertFalse([c for c in self.x.calls if c[0] == 'open'])
        self.assertNotIn('TEST-1', self.e.s['seen'])                            # it may still be entered on fresh data
        self.assertIsNone(self.on_disk()['live_inflight'])

    def test_stale_exit_is_not_sent(self):
        import executors
        from unittest.mock import patch
        self.x.open_result = dict(size=1, price=.85, fee=.01, order_id='o1', client_order_id='c1')
        self.e.set_mode('live'); self.e.control('start'); self.e.tick(self.market(), self.now)
        self.assertIn('BTC', self.e.s['positions'])     # precondition: a live position to exit
        with patch.object(executors.time, 'time', side_effect=[0.0] + [15.0] * 40):
            self.e.tick(self.market(yes_bid=.96, fetched=self.now+1), self.now+1)
        self.assertFalse([c for c in self.x.calls if c[0] == 'close']); self.assertIn('BTC', self.e.s['positions'])
        self.assertIsNone(self.on_disk()['live_inflight'])
        logged = [json.loads(line) for line in Path(self.logp).read_text().splitlines()]
        self.assertTrue(any(d.get('event') == 'order_not_sent' and d.get('intent') == 'exit' for d in logged))

    def test_live_entries_resume_once_the_log_works_again(self):
        good = self.e.log.path
        self.e.log.path = str(Path(self.temp.name) / 'missing-dir' / 'log.jsonl'); self.e.log.write('x')
        self.assertFalse(self.e.log.healthy)
        self.e.log.path = good; self.e.log.write('y')
        self.assertTrue(self.e.log.healthy); self.assertEqual(self.e.log.failures, 1)   # history kept, trading not latched

    # C4: malformed exchange replies are unknown, never "flat" or "no fill"
    def test_position_without_list_is_unknown(self):
        ex = lambda *r: LiveExecutor(FakeClient(r), verify_delay=0, sleep=lambda s: None)
        self.assertIsNone(ex((200, {})).position('T'))
        self.assertIsNone(ex((200, dict(market_positions='x'))).position('T'))
        self.assertIsNone(ex((200, dict(market_positions=[dict(ticker='T', position_fp='nan')]))).position('T'))
        for rows in ([None], [{}], [dict(position_fp='1')]):
            self.assertIsNone(ex((200, dict(market_positions=rows))).position('T'), rows)

    def test_impossible_order_responses_are_ambiguous(self):
        ex = lambda *r: LiveExecutor(FakeClient(r), verify_delay=0, sleep=lambda s: None)
        for body in ([1, 2], dict(order_id='o', fill_count='-1'), dict(order_id='o', fill_count='5'),
                     dict(order_id='o', fill_count='1', average_fill_price='nan', average_fee_paid='0.01'),
                     dict(order_id='o', fill_count='1', average_fill_price='1.5', average_fee_paid='0.01'),
                     dict(order_id='o', fill_count='1', average_fill_price='0.85', average_fee_paid='-1')):
            with self.assertRaises(LiveAmbiguous, msg=str(body)): ex((201, body)).open('T', 'yes', .85, 1)
