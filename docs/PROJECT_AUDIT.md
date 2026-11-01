# Project audit

Date: 2026-11-01. Repository `jspeer8519/kalshi_bot`, branch `claude/busy-euler-ozwo5p` (merged to `main` by the owner through `442e942`).
Reversible checkpoint before this round of work: commit `442e942`.

## What the project is
A local Flask dashboard and trading engine (Python, standard library + Flask + cryptography) for Kalshi's
15-minute crypto Up/Down markets on BTC, ETH, SOL, XRP, DOGE, HYPE and BNB. Three modes share one
decision path: PAPER (simulated fills on the live book), SHADOW (separate ledger, delayed whole-contract
depth fills), LIVE_SMALL (real IOC limit orders, always 1 contract). See `ARCHITECTURE.md`.

It is not built on a "Kalshi V5 / Process Over Profit" codebase. It is a Python port of an earlier
AutoHotkey "v5" strategy (README, "Strategy and differences from the attached scripts"); no other
reference architecture is present in the repository.

## Discovery summary
| Area | Finding |
|---|---|
| Entry point | `app.py` (Flask on 127.0.0.1:8765), started by `run.sh` / `launch.sh` |
| Strategy | `strategy.py`: every entry/exit decision, shared by all modes (enforced by `tests/test_modes.py`) |
| Execution | `executors.py` (paper/shadow simulation, LIVE_SMALL), `live.py` (signed client, IOC orders, position/balance checks) |
| Market discovery | `app.fetch_asset_market`: series `KX<COIN>15M`, soonest-closing active market, details cached 30 s; no hard-coded tickers |
| Settlement data | `indexfeed.py`: Kalshi WebSocket `cfbenchmarks_value(_5hz)` (CF Benchmarks RTI + Kalshi's 60-s average) and `trade` |
| Storage | `data/paper.sqlite3` (state JSON), `data/trade_log.jsonl` (hash-chained audit log), `data/final_minute.jsonl` (monitor) |
| Risk | daily loss/profit limits, worst-case loss guard, kill-switch, same-side cap, session risk cap, live halt on ambiguity, live cap |
| Reports | `pnlreport`, `auditcheck`, `gridreport`, `sessionreport`, `reversalreport`, `finalminutereport`, `model_eval` |
| Tests | 30+ `unittest` modules, 273 tests; all pass except `test_api`, which needs Flask (not installable in the audit sandbox; it runs on the owner's machine) |
| CI/CD | none |
| Claude skills | `.claude/skills/kalshi-desk-ops` (operations, audit, reporting rules) |

## Audit constraints (what could and could not be verified here)
- The audit sandbox cannot reach Kalshi, Kalshi's documentation site, Coinbase or PyPI (network policy). Kalshi
  hostnames and response formats were verified through an external fetcher on 2026-10-31 (`API_INTEGRATIONS.md`).
- The owner's trade data lives on the Kali machine only; all performance figures here come from reports the owner
  ran there (`BASELINE_PERFORMANCE.md`).
- No ML libraries (LightGBM, XGBoost, scikit-learn, Optuna) can be installed; the bot's own virtualenv has only Flask
  and cryptography. Research tooling is therefore pure Python (`model_eval.py`).

## Findings
All defects found, their status and the tests that cover them are in `DEFECT_REGISTER.md`.
