# Operations

## Start (PAPER; live is disabled unless explicitly enabled)
```
cd ~/kalshi && ./launch.sh
```
The app always starts in PAPER with entries paused; press Start on the dashboard (http://127.0.0.1:8765).

## Update the code
Only while paused with 0 open positions:
```
git fetch origin && git checkout main && git merge --ff-only origin/claude/busy-euler-ozwo5p && git push origin main
```
Then Ctrl+C the app and run `./launch.sh` again.

## Daily checks (read-only)
| Command | Purpose |
|---|---|
| `.venv/bin/python auditcheck.py --out output/reports/audit-$(date +%F).md` | health, reconciliation, settlements vs Kalshi |
| `.venv/bin/python sessionreport.py --since 2026-11-01` | losses vs chance, majority side, coins, settings versions, hours |
| `.venv/bin/python model_eval.py --mode shadow --since 2026-11-01` | edge with confidence intervals, calibration, walk-forward model |
| `.venv/bin/python pnlreport.py --mode shadow --period 24h` | daily P&L |
| `.venv/bin/python reversalreport.py` / `finalminutereport.py` | record-only experiments |

## Rules
- Change one trading setting at a time; settings labels split the reports by version.
- Never edit `data/paper.sqlite3` or `data/trade_log.jsonl`.
- A live halt (red banner) means: check the Kalshi account first, then clear it.
- Back up `data/` regularly (e.g. `tar czf ~/kalshi-data-$(date +%F).tgz data/`).
