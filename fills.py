"""Order-book fill model. Everything here is pure: a snapshot and an order in, a fill out.

Snapshots carry each side's bid ladder (``yes_bids``/``no_bids``, best first). A YES ask at
price p is a NO bid at 1-p, so ask ladders are derived from the other side's bids."""
import math

EPS = 1e-9


def bid_levels(m, side):
    """Bid ladder for ``side`` as [(price, size)], best first. Falls back to the best level only."""
    levels = m.get(side + '_bids')
    if levels:
        return [(float(p), float(q)) for p, q in levels]
    bid = m.get(side + '_bid')
    return [(bid, float(m.get(side + '_bid_size', 0) or 0))] if bid is not None else []


def ask_levels(m, side):
    """Ask ladder for ``side`` as [(price, size)], best (lowest) first."""
    other = m.get(('no' if side == 'yes' else 'yes') + '_bids')
    if other:
        return [(round(1 - float(p), 6), float(q)) for p, q in other]
    ask = m.get(side + '_ask')
    return [(ask, float(m.get(side + '_ask_size', 0) or 0))] if ask is not None else []


def _walk(levels, size, ok):
    filled = notional = 0.0
    worst = None
    for price, qty in levels:
        if not ok(price):
            break
        take = min(qty, size - filled)
        if take <= EPS:
            break
        filled += take; notional += take * price; worst = price
    if filled <= 0:
        return 0, None, None
    return filled, round(notional / filled, 6), worst


def walk_bids(levels, size, floor=None):
    """Sell up to ``size`` contracts into ``levels`` (best first), never below ``floor``.
    Returns (contracts filled, average price, worst price used); (0, None, None) if nothing fills."""
    return _walk(levels, size, lambda p: floor is None or p >= floor - EPS)


def walk_asks(levels, size, limit):
    """Buy up to ``size`` contracts from ``levels`` (best first), never above ``limit``."""
    return _walk(levels, size, lambda p: p <= limit + EPS)


def available(levels, ok):
    return sum(q for p, q in levels if ok(p))


def simulate(m, side, intent, size, price, depth=True, whole=False):
    """Simulated immediate-or-cancel order against snapshot ``m``.

    ``intent`` 'entry' buys ``side`` up to limit ``price``; 'exit' sells it no lower than ``price``
    (None = any bid). ``depth`` False fills entries at the best ask only (legacy paper model);
    exits always walk the bid ladder. ``whole`` rounds the filled size down to whole contracts."""
    if intent == 'entry':
        levels = ask_levels(m, side)
        if not depth:
            levels = levels[:1]
        ok = lambda p: p <= price + EPS
        walk = lambda n: walk_asks(levels, n, price)
    else:
        levels = bid_levels(m, side)
        ok = lambda p: price is None or p >= price - EPS
        walk = lambda n: walk_bids(levels, n, price)
    size = min(size, available(levels, ok))
    if whole:
        size = math.floor(size + EPS)
    if size <= EPS:
        return 0, None, None
    return walk(size)
