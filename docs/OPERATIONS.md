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

## Small-bankroll plan ($100)
Strategy lab settings (one change at a time; each starts a new settings label):
| Setting | Value | Effect |
|---|---|---|
| Position size by / Dollars per trade | Dollars / 10 | at most ~$10 per trade |
| Max $ risk per trade | 10 | hard cap: worst-case loss of any one trade |
| Max entries per 15-min session | 1 | one market per session |
| Max $ at risk per 15-min session | 10 | combined session exposure |
| Daily realized loss limit | 40 | entries stop for the UTC day after -$40 |
| Cross-coin agreement minimum | 0 | experimental; enable only as its own forward test |
Paper cash stays at $1,000 (fixed by design); these limits make exposure match a $100 bankroll.
Check with `.venv/bin/python strategylab.py --mode shadow --since <date the plan started>`.
