"""Item 9: PAPER / SHADOW / LIVE_SMALL, and proof that decisions are one shared code path."""
import inspect
import re
from unittest.mock import patch

import strategy
from engine import Engine
from helpers import CLOSE, NOW, EngineCase, FakeExecutor, market

# A session touching every decision branch: entries, a same-side-cap skip, an out-of-band market,
# a take-profit, a confirmed stop, and a market in the next session.
NO_FAVORED = dict(yes_ask=.17, yes_bid=.15, no_ask=.85, no_bid=.83)
SCENARIO = [
    (market('BTC'), NOW),
    (market('ETH'), NOW),                                                  # YES cap reached -> skip
    (market('SOL', **NO_FAVORED), NOW),                                    # NO entry
    (market('DOGE', yes_ask=.5, no_ask=.52), NOW),                         # out of band -> nothing
    (market('BTC', yes_bid=.96, fetched=NOW+2), NOW+2),                    # take profit
    (market('SOL', no_bid=.30, fetched=NOW+3), NOW+3),                     # stop starts
    (market('SOL', no_bid=.30, fetched=NOW+4), NOW+4),                     # stop confirmed
    (market('ETH', ticker='KXETH15M-T2', close=CLOSE+900, fetched=NOW+800), NOW+800),   # next session
]


def normalize(d):
    if isinstance(d, strategy.Entry): return ('entry', d.side, d.price, d.count)
    if isinstance(d, strategy.Exit): return ('exit', d.reason, d.floor, d.count)
    if isinstance(d, strategy.Skip): return ('skip', d.reason, d.key)
    return None


class SharedDecisionPathTests(EngineCase):
    def run_mode(self, mode):
        e = Engine(self.path + mode, live_enabled=True, executor=FakeExecutor())
        decisions, orders = [], []
        real_entry, real_exit = strategy.decide_entry, strategy.decide_exit

        def entry(*a, **k):
            d = real_entry(*a, **k); decisions.append((a[0], 'E', normalize(d))); return d

        def exit_(*a, **k):
            d = real_exit(*a, **k); decisions.append((a[0], 'X', normalize(d))); return d
        for ex in e.executors.values():                                   # record what every executor is asked to do
            for name in ('enter', 'exit'):
                fn = getattr(ex, name)
                setattr(ex, name, lambda *a, _fn=fn, **k: (orders.append((a[1], {x: a[-2][x] for x in ('intent', 'side', 'price', 'count', 'reason') if x in a[-2]})), _fn(*a, **k))[1])
        e.set_mode(mode); e.control('start')
        with patch.object(strategy, 'decide_entry', entry), patch.object(strategy, 'decide_exit', exit_):
            for m, now in SCENARIO:
                e.tick(dict(m), now)
        trades = [(t['asset'], t['side'], t['reason'], t['mode']) for t in e.s['trades']]
        calls = e.executors.get('live') and e.executors['live'].x.calls
        e.db.close()
        return decisions, orders, trades, calls

    def test_every_mode_makes_identical_decisions_and_orders(self):
        paper, shadow, live = (self.run_mode(m) for m in ('paper', 'shadow', 'live'))
        self.assertGreaterEqual(len([d for d in paper[0] if d[2]]), 6)         # the scenario exercises real decisions
        self.assertEqual(paper[0], shadow[0]); self.assertEqual(paper[0], live[0])
        self.assertEqual(paper[1], shadow[1]); self.assertEqual(paper[1], live[1])
        self.assertEqual([t[:3] for t in paper[2]], [t[:3] for t in live[2]])
        self.assertEqual({t[3] for t in shadow[2]}, {'shadow'}); self.assertEqual({t[3] for t in live[2]}, {'live'})
        self.assertFalse([c for c in paper[3] if c[0] in ('open', 'close')])   # only LIVE_SMALL sends orders
        self.assertFalse([c for c in shadow[3] if c[0] in ('open', 'close')])
        self.assertTrue([c for c in live[3] if c[0] == 'open'])

    def test_tick_contains_no_mode_specific_branches(self):
        src = inspect.getsource(Engine.tick)
        self.assertEqual(src.count('strategy.decide_entry'), 1); self.assertEqual(src.count('strategy.decide_exit'), 1)
        self.assertIsNone(re.search(r"'(paper|shadow|live)'|self\.mode\s*==", src))
        for name in ('_execute_entry', '_execute_exit'):
            self.assertIsNone(re.search(r"'(paper|shadow|live)'|self\.mode\s*==", inspect.getsource(getattr(Engine, name))), name)


class ModeTests(EngineCase):
    executor = None

    def setUp(self):
        self.executor = FakeExecutor(); super().setUp()

    def test_shadow_uses_real_book_simulated_fill_and_sends_nothing(self):
        self.e.set_mode('shadow'); self.cfg(contracts=11, shadow_latency_ms=500)
        self.e.control('start'); self.e.tick(market(yes_ask_size=11), NOW)
        self.e.tick(market(no_bids=[[.15, 10.5], [.14, 30]], fetched=NOW+1), NOW+1)   # book thinned while in flight
        p = self.e.s['shadow_positions']['BTC']
        self.assertEqual(p['mode'], 'shadow'); self.assertEqual((p['size'], p['entry']), (10, .85))  # whole contracts, never above the 85c limit
        self.assertEqual(self.e.s['cash'], 1000)                                # paper cash untouched
        self.assertFalse([c for c in self.executor.calls if c[0] in ('open', 'close', 'position')])
        self.assertIn('shadow_fill', [ev['event'] for ev in self.events()])
        self.assertEqual(self.e.snapshot()['mode'], 'SHADOW')

    def test_shadow_latency(self):
        self.e.set_mode('shadow'); self.cfg(shadow_latency_ms=800)
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertEqual(self.e.s['sim_orders'][0]['model'], 'shadow')
        self.e.tick(market(fetched=NOW+1), NOW+1)
        self.assertEqual(self.e.s['shadow_positions']['BTC']['mode'], 'shadow')

    def test_live_small_label_alias_and_one_contract(self):
        self.cfg(contracts=5)
        self.e.set_mode('live_small'); self.assertEqual(self.e.mode, 'live'); self.assertEqual(self.e.snapshot()['mode'], 'LIVE_SMALL')
        self.e.control('start'); self.e.tick(market(), NOW)
        self.assertEqual([c for c in self.executor.calls if c[0] == 'open'], [('open', 'KXBTC15M-T1', 'yes', .85, 1)])

    def test_restart_always_returns_to_paper(self):
        self.e.set_mode('shadow')
        other = self.make(); self.assertEqual(other.mode, 'paper'); other.db.close()

    def test_shadow_position_managed_after_switching_back_to_paper(self):
        self.e.set_mode('shadow'); self.e.control('start'); self.e.tick(market(), NOW); self.e.control('pause')
        self.e.set_mode('paper')
        self.e.tick(market(yes_bid=.96, fetched=NOW+1), NOW+1)
        self.assertEqual(self.e.s['trades'][0]['mode'], 'shadow'); self.assertEqual(self.e.s['cash'], 1000)
        self.assertAlmostEqual(self.e.equity('shadow'), 1000 + self.e.s['trades'][0]['pnl'])
