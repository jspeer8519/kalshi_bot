"""Trading decisions: the single code path shared by PAPER, SHADOW and LIVE_SMALL.

``decide_entry`` and ``decide_exit`` are called with the same inputs in every mode and return a
decision (Entry, Exit, Skip or None). They never place orders, touch cash or know which executor
will act; executors.py is the only part that differs between modes. Every input is data the
engine already holds at that moment (no look-ahead): snapshots fetched at or before ``now`` and
price ticks at or before ``now``."""
from dataclasses import dataclass, field
from typing import Callable

import fairvalue
import indicators
from fills import bid_levels, walk_bids
from settings import fee, order_count

FRESH = 10           # seconds a snapshot stays usable for decisions
SIDES = ('yes', 'no')


@dataclass(frozen=True)
class Entry:
    side: str
    price: float                 # limit price: the best ask when decided
    count: int
    inputs: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Exit:
    reason: str
    floor: object                # never sell below this (None = any bid)
    count: float                 # contracts the current book can absorb above the floor
    avg: float
    worst: float
    inputs: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Skip:
    reason: str
    message: str
    key: object = None           # de-duplication key for the activity stream
    blocked: str = None          # short reason shown on the market card
    inputs: dict = field(default_factory=dict, compare=False)


@dataclass
class Ctx:
    c: dict
    now: float
    label: str                   # mode label for messages only; never changes a decision
    running: bool
    positions: dict
    seen: object
    pnl: float                   # today's realized P&L in this mode
    limited: bool
    at_risk: float               # cost of open positions (and in-flight entries) in this mode
    side_count: Callable         # (session close, side) -> entries already made this session
    markets: dict
    spot: Callable               # asset -> [(unix second, price)]
    killed: bool


def fair_for(side, fv):
    return fv['prob'] if side == 'yes' else 1 - fv['prob']


def favorite(m):
    """'yes' or 'no' for the side the market currently favors, None on a 50/50 or missing quote."""
    bid, ask = m.get('yes_bid'), m.get('yes_ask')
    mid = (bid + ask) / 2 if bid is not None and ask is not None else ask if ask is not None else bid
    if mid is None or abs(mid - .5) < 1e-9:
        return None
    return 'yes' if mid > .5 else 'no'


def direction_votes(asset, m, x):
    """Favorite side in each other selected market of the same session, from fresh snapshots
    fetched no later than now."""
    votes = {}
    for a in x.c['assets']:
        o = x.markets.get(a)
        if a == asset or not o or o.get('close') != m['close'] or o.get('status') != 'active':
            continue
        if o['fetched'] > x.now or x.now - o['fetched'] > FRESH:
            continue
        side = favorite(o)
        if side:
            votes[a] = side
    return votes


def _quotes(m):
    return {s: {k: m.get(s+'_'+k) for k in ('ask', 'ask_size', 'bid', 'bid_size')} for s in SIDES}


def decide_entry(asset, m, x):
    c, now, t = x.c, x.now, m['ticker']
    if not (x.running and not x.killed and asset in c['assets'] and asset not in x.positions and not x.limited
            and t not in x.seen and m['status'] == 'active' and 0 < m['close'] - now <= c['window'] * 60
            and now - m['fetched'] <= FRESH):
        return None
    eligible = [s for s in SIDES if m.get(s+'_ask') is not None and c['entry_min'] <= m[s+'_ask'] <= c['entry_max']
                and order_count(c, m[s+'_ask']) >= 1 and m.get(s+'_ask_size', 0) >= order_count(c, m[s+'_ask'])]
    if not eligible:
        return None
    inputs = dict(quotes=_quotes(m), seconds_left=round(m['close'] - now, 3), fetched=m['fetched'], strike=m.get('strike'),
                  eligible=list(eligible), pnl=x.pnl, at_risk=x.at_risk)
    if c['fair_value']:
        fv = fairvalue.prob_yes(x.spot(asset), m.get('strike'), m['close'], now, c['vol_lookback_s'])
        inputs['fair_value'] = fv
        if fv is None:
            return Skip('fair_value_unavailable', f'{x.label} {asset} entry skipped: fair value unavailable (needs fresh 1-second prices, '
                        f'{fairvalue.MIN_RETURNS}+ seconds of volatility history and the market target).',
                        key=('fv_unavailable', t), blocked='Fair value unavailable', inputs=inputs)
        edges = {}
        for s in eligible:
            ask, n = m[s+'_ask'], order_count(c, m[s+'_ask'])
            edges[s] = round(fair_for(s, fv) - ask - fee(ask, n) / n, 6)
        inputs['edges'] = edges
        eligible = [s for s in eligible if edges[s] >= c['edge_margin'] - 1e-9]
        if not eligible:
            best = max(edges.values())
            return Skip('no_edge', f'{x.label} {asset} entry skipped: fair value {fv["prob"]*100:.1f}% YES leaves {best*100:+.1f}¢ edge after fees, '
                        f'below the {c["edge_margin"]*100:.1f}¢ margin.', key=('no_edge', t),
                        blocked=f'No edge: best {best*100:+.1f}¢ vs {c["edge_margin"]*100:.1f}¢ margin', inputs=inputs)
    side = max(eligible, key=lambda s: m[s+'_ask'])
    price = m[side+'_ask']; count = order_count(c, price)
    if c['max_same_side'] > 0:
        done = x.side_count(m['close'], side)
        inputs['same_side_entries'] = done
        if done >= c['max_same_side']:
            return Skip('same_side_cap', f'{x.label} {asset} entry skipped: {done} {side.upper()} entr{"y" if done == 1 else "ies"} already this session '
                        f'(cap {c["max_same_side"]:g}).', key=('same_side', t, side),
                        blocked=f'Same-side cap: {done} {side.upper()} this session', inputs=inputs)
    if c['direction_filter']:
        votes = direction_votes(asset, m, x)
        inputs['direction_votes'] = votes
        against = sorted(a for a, v in votes.items() if v != side)
        if not votes:
            return Skip('direction_no_data', f'{x.label} {asset} entry skipped: no fresh quotes from the other markets to confirm direction.',
                        key=('direction', t), blocked='Direction filter: no other market data', inputs=inputs)
        if against:
            return Skip('direction_disagree', f'{x.label} {asset} entry skipped: {side.upper()} disagrees with {", ".join(against)}.',
                        key=('direction', t, side), blocked='Direction filter: ' + ', '.join(against) + ' disagree', inputs=inputs)
    if c['bb_filter']:
        # Bollinger Bands on 1-minute closes of the settlement index (Coinbase if the index is down), data at
        # or before now only. A YES entry bets the price stays up; skip it while the price is breaking down
        # through the lower band (%B at or below the threshold), and the mirror case for NO.
        bb = indicators.bollinger_now(x.spot(asset), now, int(c['bb_period']), c['bb_k'])
        inputs['bollinger'] = bb
        if bb is None:
            return Skip('bb_unavailable', f'{x.label} {asset} entry skipped: Bollinger filter needs {int(c["bb_period"])} consecutive minutes of fresh prices.',
                        key=('bb_unavailable', t), blocked='Bollinger: not enough price history', inputs=inputs)
        against = bb['pctb'] <= c['bb_pctb'] + 1e-9 if side == 'yes' else bb['pctb'] >= 1 - c['bb_pctb'] - 1e-9
        if against:
            band = 'lower' if side == 'yes' else 'upper'
            return Skip('bb_against', f'{x.label} {asset} entry skipped: price is at/through the {band} 1-min Bollinger band (%B {bb["pctb"]:.2f}), '
                        f'moving against {side.upper()}.', key=('bb_against', t, side),
                        blocked=f'Bollinger: %B {bb["pctb"]:.2f}, {band} band break against {side.upper()}', inputs=inputs)
    need = round(price*count + fee(price, count), 6)
    if c['daily_loss'] > 0 and x.pnl - x.at_risk - need < -c['daily_loss'] - 1e-9:
        # Each binary position can lose its whole cost; never let the worst case break the daily loss limit.
        room = c['daily_loss'] + x.pnl - x.at_risk
        return Skip('daily_loss_guard', f'{x.label} {asset} entry skipped: if every open position lost, today would pass the ${c["daily_loss"]:g} daily loss limit '
                    f'(realized ${x.pnl:.2f}, at risk ${x.at_risk:.2f}, this trade ${need:.2f}).', key=('daily_loss_guard', t),
                    blocked=f'Blocked by daily loss limit: ${max(room, 0):.2f} of room left, this trade needs ${need:.2f}', inputs=inputs)
    return Entry(side, price, count, inputs)


def decide_exit(asset, p, m, x, stop_since, label='PAPER', stop_key=None):
    """Exit decision for held position ``p`` given a fresh snapshot of its open market.
    Updates ``stop_since[stop_key]`` (the stop-confirmation timer; one per ledger and market).
    ``label`` names the position's mode in messages."""
    c, now = x.c, x.now
    key = stop_key or asset
    bid = m.get(p['side']+'_bid')
    if bid is None:
        stop_since.pop(key, None)
        return None
    levels = bid_levels(m, p['side'])
    inputs = dict(bid=bid, bids=levels[:5], entry=p['entry'], size=p['size'], seconds_left=round(m['close'] - now, 3))
    reason = floor = None
    fv = fairvalue.prob_yes(x.spot(asset), m.get('strike'), m['close'], now, c['vol_lookback_s']) if c['fair_value'] else None
    if fv is not None:
        # Fair-value exits replace the stop and take-profit: sell only when the bid pays more than fair value
        # (plus the exit margin); otherwise hold to settlement.
        stop_since.pop(key, None)
        fair = fair_for(p['side'], fv)
        inputs.update(fair_value=fv, fair=fair)
        floor = fair + c['fv_exit_margin']
        if bid > floor + 1e-9:
            reason = 'fair value exit'
    else:
        # Fair value off, or unavailable right now: the stop and take-profit rules apply.
        if c['stop'] > 0 and bid < c['stop']:   # stop 0 = off: hold to settlement
            stop_since.setdefault(key, now)
        else:
            stop_since.pop(key, None)
        stop = key in stop_since and now - stop_since[key] >= c['stop_seconds']
        # Two optional take-profits: entry + increment, and a fixed price (e.g. 0.99). The lower one applies.
        targets = [v for v in (p['entry'] + c['take_profit'] if c['take_profit'] < 1 else None, c.get('tp_price') or None) if v is not None]
        target_px = min(targets) if targets else None
        if stop:
            reason = 'stop loss'          # a stop takes any available bid
        elif target_px is not None and bid >= target_px:
            reason, floor = 'take profit', target_px   # a take-profit never sells below its target
        if reason is None and (c['reversal_exit'] or c['reversal_watch']):
            # Reversal: the coin's index crossed back through the target against this position and stayed there.
            rv = indicators.reversal(x.spot(asset), m.get('strike'), p['side'], now, c['reversal_pct'], int(c['reversal_seconds']))
            if rv:
                inputs['reversal'] = rv
                if c['reversal_exit'] and bid >= c['reversal_min_bid'] - 1e-9:
                    reason, floor = 'reversal exit', c['reversal_min_bid']    # never sells below the minimum bid
                else:
                    why = 'recording only' if not c['reversal_exit'] else f'bid {bid*100:.1f}¢ below the {c["reversal_min_bid"]*100:.0f}¢ minimum; holding'
                    return Skip('reversal_signal', f'{label} {asset} reversal signal: price {rv["price"]:g} is past the target {rv["strike"]:g} against '
                                f'{p["side"].upper()} (bid {bid*100:.1f}¢; {why}).', key=('reversal', p['ticker']),
                                blocked=f'Reversal against {p["side"].upper()} · bid {bid*100:.1f}¢', inputs=inputs)
    if reason is None:
        return None
    n, avg, worst = walk_bids(levels, p['size'], floor)
    if n <= 0:
        return Skip('no_bids', f'{label} {asset} {reason}: no bids to sell into; will retry.', inputs=inputs)
    return Exit(reason, floor, n, avg, worst, inputs)
