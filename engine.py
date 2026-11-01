"""Engine: market state, accounting and persistence. Runs the same rules independently on several
15-minute markets (one position per market, one entry per market session).

Decisions come from strategy.py, one code path for every mode. executors.py is the only part that
differs between PAPER, SHADOW and LIVE_SMALL. The app always starts in PAPER.

Two ledgers: ``positions`` holds PAPER and LIVE_SMALL positions, ``shadow_positions`` holds SHADOW
ones, each at most one per market with its own list of used markets. SHADOW trades in its ledger
when it is the selected mode, or alongside PAPER/LIVE_SMALL when ``dual_shadow`` is on."""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import report
import strategy
from audit import TradeLog
from safeguards import Safeguards
from simqueue import SimQueue
from executors import PENDING, SKIPPED, LiveSmallExecutor, SimExecutor
from finalminute import FinalMinute, fm_path_for
from fills import bid_levels, walk_bids  # noqa: F401  (re-exported)
from settings import (ALL_ASSETS, DEFAULT_ASSETS, DEFAULTS, FLAGS, NON_NUMERIC, SERIES, asset_of, fee,  # noqa: F401
                      max_order_count, order_count, settings_tag, timestamp, trading_settings, validate)

MODES = ('paper', 'shadow', 'live')
LABELS = {'paper': 'PAPER', 'shadow': 'SHADOW', 'live': 'LIVE_SMALL'}
SETTLEMENT_ALERT_SECONDS = 600   # warn when a closed market has not settled after this long
SAVE_EVERY = 5                   # seconds between routine saves; trades, positions and settings save at once


def label(mode):
    return LABELS.get(mode, str(mode).upper())


class Engine(Safeguards, SimQueue, FinalMinute):
    def __init__(self, path, executor=None, log_path=None, live_max_contracts=1, spot_fn=None, index_fn=None, live_enabled=False):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(config=json.loads(json.dumps(DEFAULTS)),cash=1000,positions={},trades=[],events=[],seen=[],curve=[dict(t=time.time(),value=1000)])
        self._migrate()
        self.executor = executor
        self.sims = {'paper': SimExecutor('paper'), 'shadow': SimExecutor('shadow')}
        self.executors = dict(self.sims, **({'live': LiveSmallExecutor(executor)} if executor is not None else {}))
        self.spot_fn = spot_fn or (lambda asset: [])
        self.index_fn = index_fn or self.spot_fn  # settlement index only (no Coinbase fallback); tests pass spot_fn alone
        self.log = TradeLog(log_path)
        self.fm_path = fm_path_for(path)              # final-minute monitor file (record only)
        self.live_max_contracts = live_max_contracts
        self.live_enabled = bool(live_enabled)   # hard gate: LIVE_SMALL can only be selected when the operator enabled it (.env)
        self.mode = 'paper'                        # never persisted as live: restart is always paper
        self.running = False
        self.markets = {}                          # asset -> latest market snapshot
        self.errors = {}                           # asset -> feed error text
        self.stop_since = {}                       # ledger slot (asset, or asset/shadow) -> time the stop condition began
        self.last_issue = {}                       # ledger slot -> last repeated warning (de-duplication)
        self.blocked = {}                          # ledger slot -> why the current market's entry is blocked (dashboard)
        self._dirty, self._saved_at = False, 0.0   # unsaved trade/position changes; time of the last save
        self.record('System ready. PAPER mode; entries paused.')
        for a, p in self.s['positions'].items():
            if p.get('mode') == 'live':
                self.record(f'Open LIVE {a} position restored; it remains managed with live orders.')
        stuck = self.s['live_inflight']
        if stuck:                                  # the app stopped while a live order was unresolved
            self.s['live_inflight'] = None
            self.s['live_halt'] = self.s['live_halt'] or (f'The app restarted while a live {stuck["intent"]} order on {stuck["ticker"]} ({stuck["side"].upper()}) '
                                                          'was unresolved: check that market\'s orders and position on Kalshi.')
            self.log.write('live_halt', reason=self.s['live_halt'], inflight=stuck)
        if self.s['live_halt']:
            self.record('LIVE HALT still active: ' + self.s['live_halt'])
        for m, k in self.s['kill'].items():
            self.record(f'KILL-SWITCH still active for {label(m)}: {k["reason"]}')
        self.log.write('startup', mode='paper', open_positions=self.s['positions'], shadow_positions=self.s['shadow_positions'], live_halt=self.s['live_halt'])
        for o in self.s['sim_orders']:             # in-flight simulated orders cannot survive a restart
            self.log.write('sim_order_expired', why='restart', **self._order_fields(o))
        self.s['sim_orders'] = []
        self._apply_pending_config()
        self.save()

    def _migrate(self):
        """Upgrade state saved by earlier versions."""
        c = self.s['config']
        if 'asset' in c:                           # single-market config -> the four default markets
            c.pop('asset')
            c.setdefault('assets', list(DEFAULT_ASSETS))
        for k, v in DEFAULTS.items():
            c.setdefault(k, json.loads(json.dumps(v)))
        self.s.setdefault('positions', {})
        old = self.s.pop('position', None)
        if old:
            self.s['positions'][asset_of(old['ticker']) or c['assets'][0]] = old
        for k, v in dict(live_halt=None, live_balance=None, live_inflight=None, pending_config=None, sim_orders=[], fills={}, settle_watch=[],
                         equity_peak={}, kill={}, session_sides={}).items():
            self.s.setdefault(k, v)
        if 'shadow_positions' not in self.s:       # SHADOW positions used to share the paper ledger
            moved = [a for a, p in self.s['positions'].items() if p.get('mode') == 'shadow']
            self.s['shadow_positions'] = {a: self.s['positions'].pop(a) for a in moved}
            self.s['shadow_seen'] = list(self.s.get('seen', []))
        if self.s['pending_config'] is not None:
            for k, v in DEFAULTS.items():
                self.s['pending_config'].setdefault(k, json.loads(json.dumps(v)))

    def record(self, message):
        self.s['events'].insert(0, dict(t=time.time(),message=message))
        self.s['events'] = self.s['events'][:200]

    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)',(json.dumps(self.s),))
        self._dirty, self._saved_at = False, time.time()

    def changed(self):
        """Mark a change that must reach disk on this tick (entries, exits, used markets, settlements)."""
        self._dirty = True

    def _save_if_due(self):
        """Save now if something important changed, else at most every SAVE_EVERY seconds. Rewriting the
        whole state on every 2-second refresh of every market cost ~15% of a core and ~200 GB/day of writes."""
        if self._dirty or time.time() - self._saved_at >= SAVE_EVERY:
            self.save()

    def daily_pnl(self, now, mode=None):
        day = datetime.fromtimestamp(now,timezone.utc).date()
        mode = mode or self.mode
        return sum(t['pnl'] for t in self.s['trades'] if t.get('mode','paper')==mode and datetime.fromtimestamp(t['closed'],timezone.utc).date()==day)

    def watched_assets(self):
        """Markets to poll: the configured ones plus any with an open position."""
        assets = list(self.s['config']['assets'])
        held = [a for book in self._books() for a in book]
        return assets + [a for a in dict.fromkeys(held) if a not in assets]

    # ---- ledgers ----------------------------------------------------------------------
    def book(self, mode):
        """Positions of the ledger that ``mode`` trades in."""
        return self.s['shadow_positions'] if mode == 'shadow' else self.s['positions']

    def _books(self):
        return (self.s['positions'], self.s['shadow_positions'])

    def all_positions(self):
        return [p for book in self._books() for p in book.values()]

    def seen_list(self, mode):
        return self.s['shadow_seen'] if mode == 'shadow' else self.s['seen']

    @staticmethod
    def slot(asset, mode):
        """Key for per-ledger stop timers, warnings and blocked reasons."""
        return asset + '/shadow' if mode == 'shadow' else asset

    def entry_modes(self):
        """Ledgers taking new entries: the selected mode, plus SHADOW alongside it when enabled."""
        dual = self.s['config']['dual_shadow'] and self.mode != 'shadow'
        return [self.mode] + (['shadow'] if dual else [])

    def _executor_for(self, p):
        return self.executors.get(p.get('mode', 'paper'))

    def _clear_blocked(self, asset, new_market):
        """A market's blocked reason ends with the market, or while its ledger holds a position."""
        for mode in ('paper', 'shadow'):
            if new_market or asset in self.book(mode): self.blocked.pop(self.slot(asset, mode), None)

    @staticmethod
    def _mode_of(p):
        return p.get('mode', 'paper')

    @staticmethod
    def _label(mode):
        return label(mode)

    # ---- controls -------------------------------------------------------------------
    def control(self, action):
        with self.lock:
            if action not in ('start','pause','clear_halt','cancel_pending','reset_kill'): raise ValueError('Unknown action')
            if action == 'clear_halt':
                if not self.s['live_halt']: raise ValueError('No live halt is active')
                self.log.write('halt_cleared', reason=self.s['live_halt'])
                self.s['live_halt'] = None
                self.record('Live halt cleared after manual account check. Entries remain paused.')
            elif action == 'cancel_pending':
                if self.s['pending_config'] is None: raise ValueError('No pending settings')
                self.s['pending_config'] = None
                self.record('Pending settings discarded. Current settings unchanged.')
                self.log.write('config_pending_cancelled', mode=self.mode)
            elif action == 'reset_kill':
                k = self.s['kill'].pop(self.mode, None)
                if not k: raise ValueError('No kill-switch is active in this mode')
                eq = self.equity(self.mode)
                if eq is not None: self.s['equity_peak'][self.mode] = eq
                self.record(f'Kill-switch reset for {label(self.mode)}; peak equity re-based to {"$%.2f" % eq if eq is not None else "unknown"}. Entries remain paused.')
                self.log.write('kill_switch_reset', mode=self.mode, previous=k, new_peak=eq)
            else:
                if action == 'start' and self.mode == 'live' and self.s['live_halt']:
                    raise ValueError('Live trading is halted. Check your Kalshi account, then clear the halt.')
                if action == 'start' and self.s['kill'].get(self.mode):
                    raise ValueError('Kill-switch is active: ' + self.s['kill'][self.mode]['reason'] + '. Reset it first.')
                self.running = action == 'start'
                self.record(f'{label(self.mode)} entries enabled on {", ".join(self.s["config"]["assets"])}.' if self.running else 'New entries paused. Open positions remain managed.')
                self.log.write('entries_enabled' if self.running else 'entries_paused', mode=self.mode, assets=self.s['config']['assets'])
            self.save()

    def set_mode(self, mode):
        """Switch between PAPER, SHADOW and LIVE_SMALL. The app layer enforces credentials and confirmation for live."""
        with self.lock:
            mode = 'live' if mode == 'live_small' else mode
            if mode not in MODES: raise ValueError('Unknown mode')
            if mode == self.mode: return
            if self.running: raise ValueError('Pause entries before switching modes')
            if mode == 'live':
                if not self.live_enabled:
                    raise ValueError('Live trading is disabled. To allow LIVE_SMALL, set LIVE_TRADING_ENABLED=true in .env and restart the app.')
                if 'live' not in self.executors: raise ValueError('Live execution is not available')
                if self.s['positions']: raise ValueError('Close or settle all open positions before switching to LIVE_SMALL')
                if self.s['live_halt']: raise ValueError('Live trading is halted. Clear the halt first.')
                self._check_live_cap(self.s['config'], 'live')
            self.mode = mode
            self.stop_since = {}
            self.record(f'Mode switched to {label(mode)}. Entries paused.')
            self.log.write('mode_change', mode=mode, config=self.s['config'])
            self.save()

    def _check_live_cap(self, c, mode=None):
        if (mode or self.mode) != 'live': return
        need = self.executors['live'].effective_count(max_order_count(c))
        if need > self.live_max_contracts:
            raise ValueError(f'These settings can buy up to {need} contracts per entry, above the live cap of {self.live_max_contracts}')

    def _busy(self):
        return any(self._books()) or any(not o.get('observe') for o in self.s['sim_orders'])

    def configure(self, c):
        """Save settings. With positions open they are kept as pending and applied automatically
        once everything is flat. Returns dict(pending, changed)."""
        with self.lock:
            c = validate(c)
            self._check_live_cap(c)
            changed = [k for k in DEFAULTS if c[k] != self.s['config'].get(k)]
            if self._busy():
                self.s['pending_config'] = c
                self.record(f'Settings saved as PENDING ({", ".join(changed) or "no changes"}). They apply automatically when all positions are flat.')
                self.log.write('config_pending', mode=self.mode, config=c, changed=changed, open_positions=sorted(self.s['positions']), shadow_positions=sorted(self.s['shadow_positions']))
                self.save()
                return dict(pending=True, changed=changed)
            self._apply_config(c, 'config_saved', 'Strategy settings saved.')
            return dict(pending=False, changed=changed)

    def _apply_config(self, c, event, message):
        changed = [k for k in DEFAULTS if c[k] != self.s['config'].get(k)]
        self.s['config'] = c
        self.s['pending_config'] = None
        self.markets = {a: m for a, m in self.markets.items() if a in c['assets']}
        self.record(message)
        self.log.write(event, mode=self.mode, config=c, changed=changed)
        self.save()

    def _apply_pending_config(self):
        c = self.s['pending_config']
        if c is None or self._busy():
            return
        try:
            c = validate(json.loads(json.dumps(c)))
            self._check_live_cap(c)
        except ValueError as e:
            self.s['pending_config'] = None
            self.record(f'Pending settings discarded: {e}')
            self.log.write('config_pending_rejected', mode=self.mode, error=str(e))
            return
        self._apply_config(c, 'config_applied', 'Pending settings applied: all positions are flat.')

    def halt(self, reason):
        self.s['live_halt'] = reason
        self.running = False
        self.changed()
        self.record('LIVE HALT: ' + reason + ' Check your Kalshi account before clearing.')
        self.log.write('live_halt', reason=reason, positions=self.s['positions'])

    def feed_error(self, asset, message):
        with self.lock:
            self.errors[asset] = message
            for mode in ('paper', 'shadow'): self.stop_since.pop(self.slot(asset, mode), None)

    # ---- accounting -------------------------------------------------------------------
    def close(self, asset, price, reason, now, settlement=False, size=None, exit_fee=None, order_id=None, decision=None, mode=None):
        """Close all of an asset's position in ``mode``'s ledger, or ``size`` contracts of it (partial fills)."""
        book=self.book(mode); p=book[asset]; n=p['size'] if size is None else min(size,p['size'])
        share=n/p['size']
        f=exit_fee if exit_fee is not None else (0 if settlement else fee(price,n))
        cost=round(p['cost']*share,6); entry_fee=round(p['entry_fee']*share,6)
        proceeds=round(price*n-f,6)
        mode=p.get('mode','paper')
        trade=dict(**p,exit=price,exit_fee=f,pnl=round(proceeds-cost,6),reason=reason,closed=now)
        trade.update(size=n,cost=cost,entry_fee=entry_fee,mode=mode,asset=asset)
        if order_id: trade['exit_order_id']=order_id
        if decision: trade['exit_decision']=decision
        if not settlement and self.s['config']['settlement_tracking']:
            trade['settled_result']=None
            if not any(w['ticker']==p['ticker'] for w in self.s['settle_watch']):
                self.s['settle_watch'].append(dict(ticker=p['ticker'],asset=asset,close=p.get('close',now),since=now))
        self.s['trades'].insert(0,trade)
        if mode=='paper':
            self.s['cash']=round(self.s['cash']+proceeds,6)
            self.s['curve'].append(dict(t=now,value=self.s['cash']))
        self.log.write('trade_closed', **{k:trade.get(k) for k in ('mode','asset','ticker','side','size','entry','exit','entry_fee','exit_fee','pnl','reason','order_id','exit_order_id','exit_decision')})
        slot=self.slot(asset,mode)
        if n>=p['size']-1e-9:
            del book[asset]
            self.stop_since.pop(slot,None)
        else:
            p['size']=round(p['size']-n,6); p['cost']=round(p['cost']-cost,6); p['entry_fee']=round(p['entry_fee']-entry_fee,6)
        self.last_issue.pop(slot,None)
        self.record(f"{label(mode)} {asset} {reason}: {p['side'].upper()} {n:g} closed at {price*100:.1f}¢ · P&L ${trade['pnl']:+.2f}.")
        self.changed()

    # ---- execution (shared by every mode; only the executor differs) -------------------
    def _issue(self, asset, message, key=None):
        """Record a repeating warning once per condition. ``key`` identifies the condition when the
        text contains changing numbers (prices, amounts), so it is not re-logged every refresh.
        Returns True when the message was new."""
        key = key or message
        if key != self.last_issue.get(asset):
            self.last_issue[asset] = key
            self.record(message)
            return True
        return False

    def _refresh_live_balance(self):
        if self.executor is not None:
            b=self.executor.balance()
            if b is not None: self.s['live_balance']=b

    def _audit(self, kind, asset, m, d, order=None, mode=None):
        if self.s['config']['audit_full']:
            self.log.write(kind, mode=mode or self.mode, asset=asset, ticker=m['ticker'], decision=type(d).__name__,
                           detail={k: v for k, v in vars(d).items() if k != 'inputs'}, inputs=d.inputs,
                           order=order and {k: order.get(k) for k in ('decision','intent','side','count','price','reason')})

    def _side_count(self, mode, close, side):
        n = self.s['session_sides'].get(mode, {}).get(str(int(close)), {}).get(side, 0)
        return n + sum(1 for o in self.s['sim_orders'] if not o.get('observe') and o['intent'] == 'entry'
                       and o['model'] == mode and o['close'] == close and o['side'] == side)

    def _session_risk(self, mode, close):
        """$ at risk in one 15-minute session for one ledger: open positions plus in-flight simulated entries."""
        held = sum(q['cost'] for q in self.book(mode).values() if q.get('mode', 'paper') == mode and q.get('close') == close)
        return held + sum(round(o['price']*o['count']+fee(o['price'], o['count']), 6) for o in self.s['sim_orders']
                          if not o.get('observe') and o['intent'] == 'entry' and o['model'] == mode and o['close'] == close)

    def _limited(self, pnl):
        c = self.s['config']
        return (c['daily_loss']>0 and pnl<=-c['daily_loss']) or (c['daily_profit']>0 and pnl>=c['daily_profit'])

    def _ctx(self, now, mode=None):
        mode = mode or self.mode
        pnl = self.daily_pnl(now, mode)
        at_risk = sum(q['cost'] for q in self.book(mode).values() if q.get('mode','paper')==mode)
        at_risk += sum(round(o['price']*o['count']+fee(o['price'],o['count']),6) for o in self.s['sim_orders']
                       if not o.get('observe') and o['intent']=='entry' and o['model']==mode)
        return strategy.Ctx(c=self.s['config'], now=now, label=label(mode), running=self.running, positions=self.book(mode),
                            seen=set(self.seen_list(mode)), pnl=pnl, limited=self._limited(pnl), at_risk=at_risk,
                            side_count=lambda close, side: self._side_count(mode, close, side), markets=self.markets,
                            spot=self.spot_fn, index=self.index_fn, session_risk=lambda close: self._session_risk(mode, close), killed=bool(self.s['kill'].get(mode)))

    def _execute_entry(self, asset, m, d, now, mode):
        ex = self.executors[mode]
        order = dict(intent='entry', side=d.side, price=d.price, count=ex.effective_count(d.count),
                     decision=uuid.uuid4().hex[:12], ticker=m['ticker'], close=m['close'])
        self._audit('decision_entry', asset, m, d, order, ex.mode)
        status, fill = ex.enter(self, asset, m, order, now)
        if status == SKIPPED: return
        seen = self.seen_list(ex.mode)
        if m['ticker'] not in seen: seen.append(m['ticker']); self.changed()
        self._observe(asset, m, order, ex.mode, now)
        if status != PENDING:
            self._on_entry(asset, ex, order, fill, now, m.get(d.side+'_bid'))

    def _on_entry(self, asset, ex, order, fill, now, mark):
        self._compare(order['decision'], ex.mode, fill)
        if fill and ex.mode == 'paper' and round(fill['price']*fill['size']+fill['fee'],6) > self.s['cash']:
            fill = None                            # cash spent elsewhere while the order was in flight
        if not fill:
            if not ex.sends_orders:
                self.record(f'{label(ex.mode)} {asset} entry: no fill at {order["price"]*100:.1f}¢ (IOC). Market skipped.')
            return
        cost = round(fill['price']*fill['size']+fill['fee'], 6)
        if ex.mode == 'paper': self.s['cash'] = round(self.s['cash']-cost, 6)
        p = dict(ticker=order['ticker'], side=order['side'], entry=fill['price'], size=fill['size'], entry_fee=fill['fee'], cost=cost,
                 opened=now, mark=mark, mode=ex.mode, close=order['close'], id=order['decision'], entry_decision=order['decision'])
        tag = settings_tag(self.s['config']); p['cfg'] = tag      # which settings produced this trade (reports split by it)
        versions = self.s.setdefault('config_versions', {})
        if tag not in versions: versions[tag] = dict(first_seen=now, settings=trading_settings(self.s['config']))
        if fill.get('order_id'): p['order_id'] = fill['order_id']
        self.book(ex.mode)[asset] = p
        self.changed()
        sides = self.s['session_sides'].setdefault(ex.mode, {})
        session = sides.setdefault(str(int(order['close'])), {})
        session[order['side']] = session.get(order['side'], 0) + 1
        for k in [k for k in sides if float(k) < now - 3600]: del sides[k]
        tail = f"order {fill['order_id']}" if fill.get('order_id') else f'cost ${cost:.2f}'
        self.record(f"{label(ex.mode)} {asset} entry: {order['side'].upper()}, {fill['size']:g} contract(s) at {fill['price']*100:.1f}¢ · {tail}.")
        if not ex.sends_orders:
            self.log.write(ex.mode+'_fill', intent='entry', asset=asset, ticker=order['ticker'], side=order['side'], price=fill['price'],
                           size=fill['size'], fee=fill['fee'], cost=cost, decision=order['decision'])
        else:
            self.s['live_inflight'] = None         # the position and the cleared in-flight marker are saved together
            self.save()                            # a real position is on disk before any further network call
        ex.post_fill(self)

    def _execute_exit(self, asset, p, m, d, now):
        ex = self._executor_for(p)
        if ex is None:
            self._issue(asset, f'{label(p.get("mode"))} {asset} {d.reason}: no executor for this position; check it manually.'); return
        if any(o['asset'] == asset and o['intent'] == 'exit' and o['model'] == ex.mode and not o.get('observe') for o in self.s['sim_orders']): return
        order = dict(intent='exit', side=p['side'], price=d.floor, count=p['size'], decided=d.count, worst=d.worst, reason=d.reason,
                     decision=uuid.uuid4().hex[:12], ticker=p['ticker'], close=m['close'])
        self._audit('decision_exit', asset, m, d, order, ex.mode)
        status, fill = ex.exit(self, asset, p, m, order, now)
        if status == SKIPPED: return
        self._observe(asset, m, order, ex.mode, now)
        if status != PENDING:
            self._on_exit(asset, ex, order, fill, now)

    def _on_exit(self, asset, ex, order, fill, now):
        self._compare(order['decision'], ex.mode, fill)
        book, slot = self.book(ex.mode), self.slot(asset, ex.mode)
        p = book.get(asset)
        if not p or p['ticker'] != order['ticker']: return   # settled while the order was in flight
        if not fill:
            if not ex.sends_orders:
                self._issue(slot, f'{label(ex.mode)} {asset} {order["reason"]}: no fill; will retry.')
            return
        total = p['size']
        self.close(asset, fill['price'], order['reason'], now, size=fill['size'], exit_fee=fill['fee'], order_id=fill.get('order_id'), decision=order['decision'], mode=ex.mode)
        if asset in book:
            self._issue(slot, f'{label(ex.mode)} {asset} {order["reason"]}: only {min(fill["size"], total):g} of {total:g} contracts had bids; rest retries.')
        if ex.sends_orders:
            self.s['live_inflight'] = None         # the sale and the cleared in-flight marker are saved together
            self.save()                            # a real sale is on disk before any further network call
        ex.post_fill(self)

    def _act(self, asset, m, d, now, mode, p=None):
        if isinstance(d, strategy.Entry):
            self._execute_entry(asset, m, d, now, mode)
        elif isinstance(d, strategy.Exit):
            self._execute_exit(asset, p, m, d, now)
        elif isinstance(d, strategy.Skip):
            slot = self.slot(asset, mode)
            if d.blocked: self.blocked[slot] = d.blocked
            if self._issue(slot, d.message, key=d.key):
                self._audit('decision_skip', asset, m, d, mode=mode)
                if d.reason == 'reversal_signal' and p:     # always recorded: reversalreport.py compares it with the settlement
                    rv = d.inputs.get('reversal') or {}
                    self.log.write('reversal_signal', mode=mode, asset=asset, ticker=p['ticker'], side=p['side'], entry=p['entry'], size=p['size'],
                                   bid=d.inputs.get('bid'), price=rv.get('price'), strike=rv.get('strike'), seconds_left=d.inputs.get('seconds_left'))

    # ---- main loop ------------------------------------------------------------------
    def tick(self,m,now=None):
        """Process one fresh snapshot ``m`` for market ``m['asset']``: exits for each ledger's position,
        then entries for each ledger taking entries. Every ledger uses the same decision functions."""
        with self.lock:
            now=time.time() if now is None else now   # decision time is taken once the lock is held
            self.tick_wall=time.time()                 # wall clock at the start of this update (live quote-age check)
            c=self.s['config']
            asset=m.get('asset') or asset_of(m['ticker']) or c['assets'][0]
            prev=self.markets.get(asset)
            if prev and prev.get('ticker')==m['ticker'] and m['fetched']<prev['fetched']:return   # an older snapshot arriving late
            self.markets[asset]=m; self.errors.pop(asset,None)
            self._clear_blocked(asset,not prev or prev.get('ticker')!=m['ticker'])
            self._run_sim_orders(asset,m,now)
            for book in self._books():
                p=book.get(asset)
                if not (p and p['ticker']==m['ticker']):continue
                pm=self._mode_of(p);slot=self.slot(asset,pm)
                if m.get('result') in ('yes','no') and m.get('status') in ('settled','finalized'):
                    ex=self._executor_for(p)
                    self.close(asset,float(m['result']==p['side']),'settlement',now,True,mode=pm)
                    if ex: ex.post_fill(self)
                elif now>m['close']+SETTLEMENT_ALERT_SECONDS:
                    self._issue(slot,f'{asset} {p["ticker"]} closed {int((now-m["close"])//60)} min ago and has not settled (status {m.get("status")}, result {m.get("result") or "none"}). {asset} stays blocked until it does; check it on Kalshi.')
                elif now-m['fetched']<=strategy.FRESH and now<m['close']:
                    if m.get(p['side']+'_bid') is not None: p['mark']=m[p['side']+'_bid']
                    self._act(asset,m,strategy.decide_exit(asset,p,m,self._ctx(now,pm),self.stop_since,label(pm),slot),now,pm,p)
            if now-m['fetched']>strategy.FRESH or now>=m['close']:
                for book in self._books():
                    if asset in book:self.stop_since.pop(self.slot(asset,self._mode_of(book[asset])),None)
            pnl=self.daily_pnl(now)
            if self._limited(pnl) and self.running:
                self.running=False;self.record(f'Daily realized {label(self.mode)} P&L limit reached; entries paused on all markets.')
                self.log.write('daily_limit', mode=self.mode, pnl=pnl)
            self._kill_check(now)
            for mode in self.entry_modes():
                self._act(asset,m,strategy.decide_entry(asset,m,self._ctx(now,mode)),now,mode)
            try:self.final_minute(asset,m,now)      # record only, AFTER this update's decisions; writes go to a background thread
            except Exception as e:self._issue(asset,f'Final-minute monitor error ({type(e).__name__}); recording skipped, trading unaffected.',key='fm_error')
            self._apply_pending_config()
            self.s['seen']=self.s['seen'][-500:];self.s['shadow_seen']=self.s['shadow_seen'][-500:]
            self._save_if_due()

    def comparison_report(self):
        with self.lock:
            return report.build(json.loads(json.dumps(self.s['trades'])), json.loads(json.dumps(self.s['fills'])))

    def snapshot(self):
        with self.lock:
            result=json.loads(json.dumps({k:v for k,v in self.s.items() if k not in ('fills','sim_orders')}))
            paper=[p for p in result['positions'].values() if p.get('mode','paper')=='paper']
            marks_known=all(p.get('mark') is not None for p in paper)
            pending=self.s['pending_config']
            result.update(running=self.running,markets=json.loads(json.dumps(self.markets)),errors=dict(self.errors),blocked=dict(self.blocked),mode=label(self.mode),
                          live_available='live' in self.executors,live_max_contracts=self.live_max_contracts,live_small_contracts=LiveSmallExecutor.CONTRACTS,
                          daily_pnl=self.daily_pnl(time.time()),
                          equity=self.s['cash']+sum(p['mark']*p['size'] for p in paper) if marks_known else None,
                          equity_by_mode={m:self.equity(m) for m in MODES},kill_active=self.s['kill'].get(self.mode),
                          pending_changed=[k for k in DEFAULTS if pending[k]!=self.s['config'].get(k)] if pending else [],
                          sim_orders_in_flight=len(self.s['sim_orders']),log_failures=self.log.failures,now=time.time())
            return result
