---
name: kalshi-desk-ops
description: Operate, audit and report on the Kalshi Desk bot in /home/kali/kalshi. Use for "audit the bot", "how is shadow doing", "P&L report", "is the bot healthy", "why is the chart/feed wrong", "restart the bot", or before any change to trading, execution or chart code in this project.
---

# Kalshi Desk operations

Read `CLAUDE.md` in the project root first: its safety rules override anything here.

## Audit ("audit", "is it healthy", "check everything")
1. `.venv/bin/python auditcheck.py --out output/reports/audit-$(date +%F).md` (read-only; exit 1 = a FAIL).
2. Report every FAIL/WARN row verbatim with the action to take. Common ones:
   - Engine paused: the user presses Start on the dashboard (never call /api/control yourself).
   - Memory > 400 MB: restart with `./launch.sh` when no positions are open (`run.sh` sets MALLOC_ARENA_MAX=2).
   - Secret file permissions: `chmod 600` the named file.
   - Market feed stale or index stream not connected: check the terminal running `./launch.sh`; the index stream needs saved Kalshi credentials.
3. For a deeper audit also: memory trend (`ps -o rss=` a few times), `/api/state` size, and `pnlreport.py` data-quality section.

## Performance ("how is shadow doing", "P&L")
1. `.venv/bin/python pnlreport.py --mode shadow --period all` (and `--period 24h`), same for `--mode paper`.
2. Lead with: closed trades, win rate vs the break-even win rate, realized P&L, expectancy, max drawdown.
3. Then the two clearest patterns from the breakdown tables with numbers; flag groups under 20-30 trades as too small to trust.
4. Shadow vs paper: entry difference AND missed fills (shadow misses are the real cost of order delay).
5. These are simulations: never present them as live performance or recommend going live.

## Changing code
- Decisions belong in `strategy.py` only; mode-specific behavior only in `executors.py`. `tests/test_modes.py` enforces one shared decision path.
- New trading behavior: config flag in `settings.DEFAULTS` (default off unless the user says otherwise), validation, form field in `static/index.html`, tests.
- Anything touching live orders, positions, cash or saving: run the `optik-trading-safety-reviewer` skill on the change.
- Verify UI changes in the built-in browser against a test copy: `KALSHI_DATA_DIR=$(mktemp -d)` and a harness on port 8766 (clear `app.app.before_request_funcs[None]` in the harness only), then delete it.
- Before finishing: tests, lint, `node --check static/app.js`, and show the user a diff (no git: snapshot files to `output/` first).

## Chart accuracy
The coin-box chart must match kalshi.com: the line is Kalshi's running 60-second average of the CF Benchmarks index, from `indexfeed.py`. To verify, read Kalshi's "NOW" value from the market page with the browser and compare it with `/api/index` `avg` at the same seconds (Kalshi displays about 2 s later).
