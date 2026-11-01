# Kalshi Desk

Local Flask dashboard (127.0.0.1:8765) and bot that trades Kalshi 15-minute crypto Up/Down markets (BTC, ETH, SOL, XRP, DOGE, HYPE, BNB) in PAPER, SHADOW and LIVE_SMALL modes. Python 3.14 in `.venv`; tests use `unittest`.

## Safety rules (always)
- Never place, cancel or modify live orders, switch to LIVE_SMALL, or call the app's POST endpoints unless the user explicitly asks in this conversation.
- LIVE_SMALL is impossible unless `.env` has `LIVE_TRADING_ENABLED=true` (`Engine(live_enabled=...)`, default off). Never set it or suggest bypassing it; tests that need live pass `live_enabled=True` explicitly.
- Never open, print or quote `.env`, `kalshi_private_key.pem` or anything in `.private/`. Credential loading in `app.py` (`load_credentials` and friends) and `live.py` must not change without explicit approval.
- Don't restart the running app (`python app.py`) without asking; it may hold open positions. Test with a copy on another port and a temporary `KALSHI_DATA_DIR`.
- The state DB (`data/paper.sqlite3`) and audit log (`data/trade_log.jsonl`) are the source of truth: read them read-only (`sqlite3 'file:...?mode=ro'`), never edit them.
- Every behavior change needs a test; new trading features are config flags, default off.

## Architecture
- `strategy.py`: every entry/exit decision, one code path for all modes. `executors.py`: the only mode-specific part (SimExecutor paper/shadow, LiveSmallExecutor = 1 contract IOC).
- `engine.py`: state, accounting, two ledgers (`positions` for paper/live, `shadow_positions`), pending settings, save throttling (immediate save on trades, positions, used markets, halts; else every 5 s). Mixins: `safeguards.py` (equity, kill-switch, settlement tracking), `simqueue.py` (simulated latency, fill comparison).
- `settings.py` (DEFAULTS, validate, fee), `fills.py` (order-book fill model), `fairvalue.py` (P(YES) model), `audit.py` (hash-chained log), `report.py` (fill comparison).
- `indexfeed.py`: Kalshi WebSocket (signed, read-only) streaming CF Benchmarks index (5 Hz + 1/s with Kalshi's 60-second average) and public trades. `quotes.py`: contract price history. `pricefeed.py`: Coinbase 1-second ticks (fallback).
- `app.py`: Flask routes, market feed loop, settlement checks, on-demand book fetch for due simulated orders.

## Facts verified against Kalshi (keep them true)
- Settlement: YES if the simple average of the 60 one-second CF Benchmarks RTI values before close is at least the 60-second average before open (`floor_strike`); ties resolve YES.
- Kalshi's market-page chart line ("NOW") is that running 60-second average (`avg_60s_data` on the `cfbenchmarks_value` channel). The dashboard draws the same series; `auditcheck.py` compares its value at close with `expiration_value`.
- Index IDs: BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI, DOGEUSD_RTI (5 Hz), HYPEUSD_RTI, BNBUSD_RTI (1/s only).

## Commands
- Tests: `.venv/bin/python -m unittest discover -s tests`
- Lint: `ruff check --select F,E9,B --ignore E701,E702,E731 *.py tests/`
- JS syntax: `node --check static/app.js`
- Audit (read-only): `.venv/bin/python auditcheck.py [--no-network] --out output/reports/audit-$(date +%F).md`
- P&L (read-only): `.venv/bin/python pnlreport.py --mode shadow|paper|live --period all|24h|7d|mtd`
- Research (read-only): `sessionreport.py --since YYYY-MM-DD`, `gridreport.py`, `model_eval.py --mode shadow` (edge with confidence intervals, calibration, walk-forward model), `reversalreport.py`, `finalminutereport.py`
- Docs: `docs/` (audit, architecture, defect and risk registers, baseline, strategy research, readiness). Update `DEFECT_REGISTER.md` when fixing a defect.
- Start the app (user's terminal): `./launch.sh`
