"""Safeguards mixed into Engine: per-mode equity, the drawdown kill-switch, and settlement
tracking for positions that were exited before their market settled."""
import time

SETTLE_CHECK_SECONDS = 30        # how often to ask Kalshi for an early-exited market's result
SETTLE_GIVE_UP = 86400


class Safeguards:
    def equity(self, mode):
        """Mark-to-market equity of one mode's ledger (positions at their last bid), or None if unknown."""
        pos = [p for p in self.all_positions() if p.get('mode', 'paper') == mode]
        value = sum((p['mark'] if p.get('mark') is not None else p['entry']) * p['size'] for p in pos)
        if mode == 'paper':
            return round(self.s['cash'] + value, 6)
        if mode == 'shadow':
            realized = sum(t['pnl'] for t in self.s['trades'] if t.get('mode') == 'shadow')
            return round(self.s['config']['initial_balance'] + realized + value - sum(p['cost'] for p in pos), 6)
        return None if self.s['live_balance'] is None else round(self.s['live_balance'] + value, 6)

    def _kill_check(self, now):
        c = self.s['config']
        if not c['kill_switch']: return
        for mode in self.entry_modes():
            self._kill_check_mode(mode, now, c)

    def _kill_check_mode(self, mode, now, c):
        """A tripped side ledger (SHADOW alongside PAPER) only stops its own entries."""
        eq = self.equity(mode)
        if eq is None: return
        peak = max(self.s['equity_peak'].get(mode, eq), eq)
        self.s['equity_peak'][mode] = peak
        if not self.s['kill'].get(mode) and peak > 0 and eq <= peak * (1 - c['kill_switch_pct']) + 1e-9:
            reason = f'equity ${eq:.2f} is {1 - eq / peak:.1%} below peak ${peak:.2f} (limit {c["kill_switch_pct"]:.0%})'
            self.s['kill'][mode] = dict(reason=reason, at=now, equity=eq, peak=peak)
            if mode == self.mode: self.running = False
            self.changed()
            self.record(f'KILL-SWITCH ({self._label(mode)}): {reason}. New entries halted until you reset it; open positions stay managed.')
            self.log.write('kill_switch', mode=mode, equity=eq, peak=peak, limit=c['kill_switch_pct'])

    # ---- settlement tracking for early exits -----------------------------------------
    def settlement_due(self, now=None):
        """Tickers of early-exited markets whose result should be checked now."""
        now = time.time() if now is None else now
        with self.lock:
            return [w['ticker'] for w in self.s['settle_watch'] if now >= w['close'] and now - w.get('checked', 0) >= SETTLE_CHECK_SECONDS]

    def record_settlement(self, ticker, status, result, now=None):
        now = time.time() if now is None else now
        with self.lock:
            w = next((w for w in self.s['settle_watch'] if w['ticker'] == ticker), None)
            if not w: return
            w['checked'] = now
            if status in ('settled', 'finalized') and result in ('yes', 'no'):
                diff = 0.0
                for t in self.s['trades']:
                    if t['ticker'] == ticker and 'settled_result' in t and t['settled_result'] is None:
                        held = float(result == t['side'])
                        t.update(settled_result=result, hold_value=held, hold_pnl=round(held*t['size']-t['cost'], 6))
                        t['exit_vs_hold'] = round(t['pnl'] - t['hold_pnl'], 6); diff += t['exit_vs_hold']
                self.s['settle_watch'].remove(w)
                self.record(f'{w["asset"]} {ticker} settled {result.upper()}: the early exit did ${diff:+.2f} versus holding to settlement.')
                self.log.write('settlement_recorded', asset=w['asset'], ticker=ticker, result=result, exit_vs_hold=round(diff, 6))
            elif now - w['close'] > SETTLE_GIVE_UP:
                self.s['settle_watch'].remove(w)
                self.log.write('settlement_untracked', asset=w['asset'], ticker=ticker, status=status)
            self.save()

