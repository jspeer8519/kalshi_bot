"""Per-trade fill comparison: paper vs shadow vs live fill, slippage and P&L difference.

Built from the comparison records the engine keeps when ``fill_comparison`` is on: for every
executed decision it stores the quoted reference price and the fill each model got for the same
order on the same book (paper and shadow simulated, live from Kalshi when in LIVE_SMALL).

All prices and P&L are per contract, so models that filled different sizes compare fairly.
Slippage is signed so that positive is worse: paid above the quoted ask on entry, received
below the quoted bid on exit. Settlement exits pay the same $1/$0 to every model, with no fee."""
import csv
import io

MODELS = ('paper', 'shadow', 'live')


def _fill(rec, model):
    f = rec.get(model) if rec else None
    return f if f and f.get('size') else None


def _r(v):
    return None if v is None else round(v, 6)


def build(trades, fills):
    rows = []
    for t in trades:
        e = fills.get(t.get('entry_decision'))
        if not e:
            continue
        settled = t['reason'] == 'settlement'
        x = fills.get(t.get('exit_decision'))
        row = dict(closed=t['closed'], asset=t.get('asset'), ticker=t['ticker'], side=t['side'], mode=t.get('mode', 'paper'),
                   reason=t['reason'], contracts=t['size'], entry_ref=e.get('ref'),
                   exit_ref=t['exit'] if settled else (x or {}).get('ref'))
        pnl = {}
        for m in MODELS:
            ef = _fill(e, m)
            xf = None if settled else _fill(x, m)
            ep = ef['price'] if ef else None
            xp = t['exit'] if settled and ef else (xf['price'] if xf else None)
            row[m+'_entry'] = ep
            row[m+'_exit'] = xp
            row[m+'_entry_slip'] = _r(ep - e['ref']) if ep is not None and e.get('ref') is not None else None
            row[m+'_exit_slip'] = _r(row['exit_ref'] - xp) if xp is not None and row['exit_ref'] is not None and not settled else (0.0 if settled and ef else None)
            if ep is not None and xp is not None:
                fees = ef['fee'] / ef['size'] + (0 if settled else xf['fee'] / xf['size'])
                pnl[m] = _r(xp - ep - fees)
            row[m+'_pnl_per_contract'] = pnl.get(m)
        for a, b in (('shadow', 'paper'), ('live', 'paper'), ('live', 'shadow')):
            row[f'{a}_vs_{b}'] = _r(pnl[a] - pnl[b]) if a in pnl and b in pnl else None
        rows.append(row)
    return rows


FIELDS = (['closed', 'asset', 'ticker', 'side', 'mode', 'reason', 'contracts', 'entry_ref', 'exit_ref']
          + [f'{m}_{k}' for m in MODELS for k in ('entry', 'exit', 'entry_slip', 'exit_slip', 'pnl_per_contract')]
          + ['shadow_vs_paper', 'live_vs_paper', 'live_vs_shadow'])


def to_csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: '' if r.get(k) is None else r[k] for k in FIELDS})
    return out.getvalue()
