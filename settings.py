"""Strategy settings: defaults, validation, fee and sizing rules shared by every mode."""
import math
from datetime import datetime
from decimal import Decimal, ROUND_CEILING

ALL_ASSETS = ['BTC','ETH','SOL','XRP','DOGE','HYPE','BNB']
SERIES = {a: 'KX'+a+'15M' for a in ALL_ASSETS}
DEFAULT_ASSETS = ['BTC','ETH','SOL','DOGE']
DEFAULTS = dict(initial_balance=1000, entry_min=.80, entry_max=.93, stop=.40,
                take_profit=.10, window=8, contracts=1, stop_seconds=1,
                daily_profit=400, daily_loss=200, assets=list(DEFAULT_ASSETS), size_mode='contracts', dollars=20,
                tp_price=0,
                # Paper realism (simulated fills). Off = fill instantly at the best ask, fractional sizes allowed.
                paper_whole_contracts=False, paper_depth_fill=False, paper_latency_ms=0,
                shadow_latency_ms=0,           # SHADOW always walks depth with whole contracts; this adds latency
                dual_shadow=False,             # also run the SHADOW ledger alongside PAPER or LIVE_SMALL
                max_same_side=1,               # max entries on one side (YES/NO) per 15-minute session; 0 = no cap
                direction_filter=False,        # skip entries the other markets' favorites disagree with
                fair_value=False, edge_margin=.02, fv_exit_margin=0, vol_lookback_s=900,
                settlement_tracking=False,     # record how early-exited markets actually settled
                kill_switch=False, kill_switch_pct=.20,
                audit_full=False,              # also log every decision with its inputs
                fill_comparison=False)         # simulate paper and shadow fills next to every real decision
FLAGS = ('paper_whole_contracts','paper_depth_fill','dual_shadow','direction_filter','fair_value','settlement_tracking',
         'kill_switch','audit_full','fill_comparison')
NON_NUMERIC = ('assets', 'size_mode') + FLAGS


def fee(price, size):
    return float((Decimal('.07')*Decimal(str(size))*Decimal(str(price))*(1-Decimal(str(price)))).quantize(Decimal('.01'), rounding=ROUND_CEILING))


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def asset_of(ticker):
    for a, series in SERIES.items():
        if ticker == series or ticker.startswith(series + '-'):
            return a
    return None


def order_count(c, price):
    """Contracts to buy at ``price``. In dollar mode: the most whole contracts whose
    cost including the estimated entry fee fits inside the dollar budget (0 if none)."""
    if c['size_mode'] == 'contracts':
        return int(c['contracts'])
    n = int(c['dollars'] // price)
    while n > 0 and price*n + fee(price, n) > c['dollars'] + 1e-9:
        n -= 1
    return n


def max_order_count(c):
    """Upper bound on contracts per entry for these settings."""
    return int(c['contracts']) if c['size_mode'] == 'contracts' else int(c['dollars'] // c['entry_min'])


def validate(c):
    if set(c) != set(DEFAULTS): raise ValueError('Unexpected or missing strategy setting')
    for k in DEFAULTS:
        if k not in NON_NUMERIC and (type(c[k]) not in (int,float) or not math.isfinite(c[k])): raise ValueError('Settings must be finite numbers')
    if any(type(c[k]) is not bool for k in FLAGS): raise ValueError('On/off settings must be true or false')
    if type(c['assets']) is not list or not c['assets'] or len(set(c['assets'])) != len(c['assets']) or any(a not in SERIES for a in c['assets']):
        raise ValueError('Choose at least one supported market')
    c['assets'] = [a for a in ALL_ASSETS if a in c['assets']]   # stable display order
    if c['size_mode'] not in ('contracts','dollars'): raise ValueError('Position size must be by contracts or dollars')
    if not 1 <= c['dollars'] <= 1000: raise ValueError('Dollars per trade must be from $1 to $1,000')
    if not 0 <= c['stop'] < c['entry_min'] <= c['entry_max'] < 1: raise ValueError('Require stop (0 = off) < entry minimum ≤ entry maximum < 1')
    if not 0 < c['take_profit'] <= 1 or not 0 < c['window'] <= 15: raise ValueError('Invalid target or entry window')
    if c['tp_price'] != 0 and not c['entry_max'] < c['tp_price'] < 1: raise ValueError('Take-profit price must be 0 (off) or above the entry maximum and below $1')
    if not 1 <= c['contracts'] <= 100 or int(c['contracts']) != c['contracts']: raise ValueError('Contracts must be an integer from 1 to 100')
    if not 0 <= c['stop_seconds'] <= 30: raise ValueError('Stop confirmation must be 0–30 seconds')
    if c['initial_balance'] != 1000: raise ValueError('Starting paper balance is fixed at $1,000')
    if not 0 <= c['daily_loss'] <= 1000 or not 0 <= c['daily_profit'] <= 10000: raise ValueError('Invalid daily limits')
    if not 0 <= c['paper_latency_ms'] <= 5000 or not 0 <= c['shadow_latency_ms'] <= 5000: raise ValueError('Simulated latency must be 0–5000 ms')
    if not 0 <= c['max_same_side'] <= len(ALL_ASSETS) or int(c['max_same_side']) != c['max_same_side']: raise ValueError('Same-side cap must be a whole number from 0 (off) to 7')
    if not 0 <= c['edge_margin'] <= .5 or not 0 <= c['fv_exit_margin'] <= .5: raise ValueError('Fair-value margins must be $0–$0.50')
    if not 60 <= c['vol_lookback_s'] <= 1200: raise ValueError('Volatility lookback must be 60–1200 seconds')
    if not .01 <= c['kill_switch_pct'] <= .99: raise ValueError('Kill-switch drawdown must be 1%–99%')
    return c
