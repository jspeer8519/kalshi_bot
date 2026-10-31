# Kalshi Desk for Kali Linux

A local web UI and Python port of the supplied Kalshi v5 AutoHotkey strategy. No Windows tools required. Real market data, SQLite persistence, strategy controls, a trade journal with CSV export, an append-only trade log, and a **Paper / Live** switch on the dashboard. The app always starts in Paper mode; real orders are sent only after you switch to Live and confirm.

## Start

```bash
cd /home/kali/kalshi
./launch.sh
```

Or run `./run.sh`, then open http://127.0.0.1:8765. The launch script opens your browser; keep its terminal open. Ctrl+C stops the app. `Kalshi-Desk.desktop` is a launcher for this checkout; copy it to your desktop and mark it trusted if you want a desktop shortcut. It is not installed automatically.

Python 3.10+ is required. On a new Kali install, install `python3-venv` if needed. `run.sh` creates a virtual environment and installs `requirements.txt` on the first run (internet required). This workspace already has a virtual environment using the installed system Flask and cryptography packages.

## Keys

The attached files contain AutoHotkey code, not a populated key ID or private-key PEM. To connect, create a key in Kalshi Profile Settings → API Keys. Open **Connection** in the UI, enter the key ID, choose the downloaded PEM/text key file, and save. Then select **Test account connection**. RSA and Ed25519 keys are supported. The balance request is read-only and never starts trading.

Credentials are stored in `.private/credentials.json` with file mode 0600 inside a 0700 directory, excluded from Git. Storage is plaintext protected by OS permissions, not an encrypted vault. Keys never appear in API state, UI responses, exports, or logs. No credentials are needed for public market data. Never put a private key in source code or a chat message.

## Markets

By default the engine trades four Kalshi 15-minute Up/Down markets side by side: **BTC, ETH, SOL and DOGE** (choose any of BTC, ETH, SOL, XRP, DOGE, HYPE, BNB under *Strategy lab → Markets traded*). Each market runs the same rules on its own: at most one position and one entry per 15-minute session per market. Paper cash and the daily profit/loss limits are shared across all markets. The feed refreshes all markets in parallel about every 2 seconds (one order-book request per market; market details are reused for up to 30 seconds and never past close). One market's feed failing never blocks the others.

**Worst-case loss guard:** a binary contract can lose its entire cost, so before every entry the engine checks today's realized P&L minus the cost of every open position minus the new trade. If that worst case would pass the daily loss limit, the entry is skipped and logged (the market can still be entered later in the session if room frees up). Example: $30 per trade on seven markets puts up to about $210 at risk at once, so with a $200 daily loss limit the seventh entry is skipped.

**Settlement alert:** if a held market is still unsettled 10 minutes after it closed, the activity stream says so; that market's asset cannot trade again until it settles.

## Modes: PAPER / SHADOW / LIVE_SMALL

The **Paper | Shadow | Live · 1 contract** switch sits on the Overview page next to the Start button.

- **PAPER** (default): simulated fills from the live order book against $1,000 of paper cash. Your Kalshi account is never touched.
- **SHADOW**: the real order book, a simulated fill, and no orders sent. Shadow positions live in their own ledger, so with `dual_shadow` on they run alongside PAPER or LIVE_SMALL. Fills always walk book depth with whole contracts, plus `shadow_latency_ms`. Shadow trades keep their own ledger (`$1,000 + realized + unrealized`) and never change paper cash.
- **LIVE_SMALL**: real immediate-or-cancel (IOC) limit orders through Kalshi's V2 order endpoint (`POST /portfolio/events/orders`), **always exactly 1 contract per entry**, whatever the position size setting. Exits sell the whole held position (reduce-only).

**One decision path.** `strategy.py` makes every entry and exit decision with the same inputs in every mode. `executors.py` is the only part that differs. `tests/test_modes.py` proves it: it runs one scenario through all three modes and asserts identical decisions and identical orders handed to the executors. It also checks that `Engine.tick` contains no mode-specific branch.

Switching to LIVE_SMALL requires all of the following:
1. Credentials saved on **Connection** (or `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY` in `.env`).
2. A successful **Test account connection** within the last 15 minutes.
3. Entries paused and no open position.
4. Typing `LIVE` in the confirmation box.
5. LIVE_SMALL's single contract within the live cap, `KALSHI_LIVE_MAX_CONTRACTS` in `.env` (default **1**, so always satisfied).

Switching to SHADOW only requires entries to be paused. Live mode then still waits for you to press **Start LIVE engine**. Switching back to Paper is always allowed. **Restarting the app always returns to Paper.**

How live orders behave:
- Before an entry: checks your Kalshi position on that market (skips if you already hold one, or if the check fails) and your cash balance.
- The market is marked as used *before* the order is sent, so a crash or retry can never double-enter it.
- Entries and exits are IOC at the quoted price, so nothing is left resting on the book. Unfilled = no trade. Partial fills are recorded at their actual size.
- Exits are `reduce_only`, so they can never open a new or opposite position.
- Entry price, size and fees come from Kalshi's order response; the resulting position is verified against your account.
- **Halt:** if Kalshi's answer is unclear (timeout, 5xx, incomplete response, position mismatch), all live orders stop, entries pause, and a red banner appears. Check your Kalshi account, then press *I checked Kalshi, clear halt*. A halt survives restarts.
- A live position that is open when the app restarts is still managed (stop, target, settlement) with live orders, even though the app comes back in Paper mode.
- Live trades do not change paper cash. Daily profit/loss limits are tracked separately for Paper and Live.
- No automatic transfers between Kalshi exchange shards. If an order is rejected for insufficient funds on the market's shard, the market is skipped and the reason is logged; move funds in Kalshi yourself.

## Market cards and indicators

Each selected market has a card on Overview: the last hour of 1-minute spot candles from Coinbase's public data (BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD, HYPE-USD, BNB-USD; no key needed), the current Kalshi market (shaded) and its target price (dashed, when Kalshi provides `floor_strike`), UP/DOWN asks and bids, a countdown, and a status line explaining what the engine is waiting for. Kalshi settles these markets on CF Benchmarks' index, so Coinbase is a close reference, not the settlement price.

Each card also has a **Kalshi contract price** chart on a fixed 0–100¢ scale, covering the current market from open to close (`quotes.py`). It shows the UP (YES) ask and the DOWN (NO) ask, which is the price the bot would pay. Points come from the order-book snapshots the feed already takes (about every 2 s, solid lines). Anything before the app started comes from Kalshi's public 1-minute candlesticks (dashed). Your entry range and entry window are shaded, and each ledger's entries (filled dots) and early exits (hollow dots) are plotted at their fill prices: green for paper, blue for shadow, red for live. The chart is display only; it never changes a decision.

**Settlement index (same data as Kalshi Pro).** With credentials saved, `indexfeed.py` opens Kalshi's official WebSocket (`wss://external-api-ws.kalshi.com/trade-api/ws/v2`), signed with the saved key, read-only. It subscribes to `cfbenchmarks_value_5hz` (up to 5 values/s for BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI, DOGEUSD_RTI), `cfbenchmarks_value` (the once-per-second values the markets settle on, for all 7 coins including HYPEUSD_RTI and BNBUSD_RTI, plus Kalshi's running 60-second settlement average) and `trade` (every trade in the current markets; earlier trades are back-filled from `GET /markets/trades`). The price chart then draws **the same line Kalshi's market page draws**: the running 60-second average of that index ("NOW"), which is the value the market settles on at close. It's verified against kalshi.com to the cent. Styling matches Kalshi: coin-coloured line with shaded area, prices on the right, time labels below, red dashed TARGET (pinned with an arrow when off-screen), % vs target at the current point, **LIVE (30 s) / 5M / 15M** ranges, a **$ / %** axis switch, and **▮▯** to show the raw index as 1-second candles. LIVE and 5M zoom to the price (the target is marked above or below when off-screen). In the final minute, "vs target" uses Kalshi's own 60-second average. The contract chart shows the trades as faint bubbles (5-second buckets per taker side, sized by volume). The fair-value model uses the index when available, else Coinbase. Without credentials, or while the feed is down, the chart falls back to Coinbase and says so. **1H** shows Coinbase 1-minute candles, because the index feed keeps 20 minutes. Keys and signatures are never logged.

The spot price chart views:
- **1s live** (default): a 1-second price line across the current 15-minute market, from open to close, with the target price dashed. The headline shows how far price is above or below the target. `pricefeed.py` polls Coinbase's public ticker once per second per market in one background thread (one request/second per selected market, so 7 with all seven selected; Coinbase's public limit is 10/second per IP, bursts to 15), and every browser tab reads from that shared buffer. The dashboard refreshes once per second. Ticks are kept in memory for 20 minutes and are lost on restart; the part of a market before the app started is drawn in grey from 1-minute closes.
  - **Target line:** Kalshi's target is *not* the price at the open. Per the market rules it is the simple average of CF Benchmarks' Real Time Index over the 60 seconds before the open, and the market settles on the same 60-second average before close. The chart shades that pre-open minute. Because Coinbase spot differs slightly from the CF index, the line will not start exactly on the target; the app measures the Coinbase/index gap over that minute and uses it to adjust "vs target" (shown as "unadjusted" if the app was not running for that minute). In the final minute, "vs target" uses the running 60-second average, matching how settlement works. This is display only; the engine trades on Kalshi's prices.
- **1m candles**: the last hour of real Coinbase 1-minute candles (loaded at once from Coinbase's history, refreshed every 10 seconds) with EMA 9 / EMA 21. The newest candle is live: it is updated every second from the 1-second feed, and a new candle starts at each minute boundary. EMA/RSI follow the 10-second refresh.

Kalshi UP/DOWN quotes are polled per market about every 2 seconds.

Indicators (`indicators.py`, computed on the server over up to 300 minutes of history, textbook definitions, no tuning):
- **EMA 9 / EMA 21** on the candles: short-term momentum; EMA 9 crossing EMA 21 is a common reversal signal.
- **RSI 14** (Wilder) in the strip below: momentum strength; above 70 / below 30 are the conventional overbought/oversold reversal-risk zones.
- **Distance to target in 1-minute ATR 14 units**: how many typical one-minute moves the price is from the market's target.

The indicators are **display only**; they do not change when the engine enters or exits.

The paper equity curve moved to the Trade journal page.

## Saving settings while positions are open

Settings that pass validation are saved immediately when nothing is open. If any position (or in-flight simulated order) is open, they are kept as **pending**: a banner on Overview and Strategy lab lists the changed settings, and they apply automatically the moment every position is flat (they are re-validated first). *Discard pending* drops them. Invalid settings are still rejected immediately. Pending settings survive restarts.

## Execution & risk features

All are set under *Strategy lab → Execution & risk features* and are **off by default**, except the same-side cap (default 1).

| Setting | Default | What it does |
|---|---|---|
| `paper_whole_contracts` | off | Paper fills round down to whole contracts. |
| `paper_depth_fill` | off | Paper entries walk the ask ladder (derived from the other side's bids) up to the limit price, instead of filling only at the best ask. Exits always walk bids. |
| `paper_latency_ms` / `shadow_latency_ms` | 0 | A simulated order fills against the order book fetched when it comes due: the app requests that market's book at `now + latency` (one request, typically 250–300 ms more), instead of waiting for the next 2-second refresh. If the book moved past the limit it does not fill (IOC), and the market is used. In-flight orders count toward risk and the same-side cap. They expire if the market closes or the app restarts. |
| `dual_shadow` | off | Runs the SHADOW ledger alongside PAPER (or LIVE_SMALL). Both ledgers make the same decisions on the same snapshots, but each keeps its own positions, used markets, stop timers, P&L and kill-switch. Each coin box shows a panel per ledger (position, bid, unrealized P&L; or how it closed this market; or why it's flat), and the 1-second chart marks entries (solid) and exits (dashed) in green for paper, blue for shadow, red for live. Only the paper ledger spends paper cash, and only LIVE_SMALL sends orders. |
| `max_same_side` | **1** | At most N entries on the same side (YES or NO) per 15-minute session across all markets, counted per mode, including positions already closed this session. 0 = no cap. |
| `direction_filter` | off | At entry, reads the favorite side (YES if the mid is above 50¢) in the other selected markets of the same session. It uses only snapshots fetched at or before that moment and no more than 10 s old, and skips the entry if any of them disagrees or none is available. |
| `fair_value`, `edge_margin`, `fv_exit_margin`, `vol_lookback_s` | off, 2¢, 0, 900 s | See *Fair-value model* below. |
| `settlement_tracking` | off | For every stop, take-profit or fair-value exit, records how the market actually settled: result, what holding would have paid (`hold_pnl`), and `exit_vs_hold`. Shown in the journal's *Settled* column and in the CSV. |
| `kill_switch`, `kill_switch_pct` | off, 20% | Halts new entries when the current mode's mark-to-market equity falls the set fraction below its peak. Exits stay managed. The halt survives restarts. *Reset kill-switch* re-bases the peak; you still have to press Start. |
| `audit_full` | off | Also logs every decision with its inputs (quotes, fair value, votes, risk numbers). Repeated skips are logged once per condition. |
| `fill_comparison` | off | For every executed decision, stores the quoted price and the fill each model got for the same order on the same book: paper and shadow simulated, live from Kalshi in LIVE_SMALL. See *Comparison report*. |

### Fair-value model

`fairvalue.py` estimates P(settle YES). **Settlement rule**, verified 2026-10-31 from Kalshi's own contract text: `GET /markets/{ticker}` returns `rules_primary`/`rules_secondary` for KXBTC15M, KXETH15M, KXSOL15M and KXDOGE15M, with settlement source CF Benchmarks and terms in assets.kalshi.com/regulatory/product-certifications/CRYPTO.pdf. YES if the simple average of the 60 one-second CF Benchmarks RTI values before close is **at least** the 60-second average before open (`floor_strike`); ties resolve YES.

The app has no CF Benchmarks feed, so the model uses the 1-second Coinbase ticks, corrected by the Coinbase/index gap measured over the pre-open minute (the same correction as the chart). It is shown as unadjusted if the app missed that minute. Model: zero-drift lognormal price, with variance per second from realized 1-second returns over `vol_lookback_s`. The final average has log-variance `var × (seconds until the final minute + 60/3)`. Inside the final minute, the seconds already observed are fixed and only the rest is random. Only ticks at or before the decision time are used. If the inputs are insufficient (stale price, under 60 s of returns, no target), entries are skipped.

- **Entry**: only if `fair − ask − fee per contract ≥ edge_margin`, in addition to the normal entry rules.
- **Exit**: sell when `bid > fair + fv_exit_margin`, never below that floor; otherwise hold to settlement. While the model is available it replaces the stop and take-profit. When it is unavailable, the stop and take-profit apply.

### Comparison report

**Trade journal → Fill comparison (CSV)** (`/api/report.csv`, JSON at `/api/report`): one row per trade, with paper, shadow and live entry/exit prices, per-contract slippage against the quoted price (positive = worse), per-contract P&L for each model, and the differences shadow − paper, live − paper and live − shadow. Settlement exits pay every model the same $1/$0. It requires `fill_comparison`.

## Audit log

Every startup, mode change, setting change (saved / pending / applied / discarded), order submission, order result (filled / no fill / rejected / ambiguous), simulated order and fill, closed trade, settlement record, daily-limit stop, kill-switch and halt is appended to `data/trade_log.jsonl`. Each entry is one JSON object per line with a UTC timestamp, written with fsync and owner-only permissions. With `audit_full`, decisions and their inputs are logged too. Each line carries `seq` and `prev` (SHA-256 of the previous line), so editing, deleting or reordering lines is detectable with `python -c "import audit; print(audit.verify('data/trade_log.jsonl'))"`. Fields named like credentials (`pem`, `key_id`, `signature`, `KALSHI-ACCESS-*`, …) and any PEM-looking string are redacted before writing. Download it from **Trade journal → Trade log (JSONL)**. The journal table and CSV export now include a Mode column and Kalshi order IDs.

## Strategy and differences from the attached scripts

Defaults come from the newer LIVE attachment. The rules below apply to both modes; fill and fee notes describe Paper mode:

- BTC / KXBTC15M; entry in the final 8 minutes.
- YES or NO ask between $0.80 and $0.93 inclusive; if both qualify, use the higher ask (YES on ties).
- Position size: either a fixed number of contracts, or **dollars per trade**. In dollar mode the engine buys the most whole contracts whose cost (ask × contracts + estimated entry fee) fits the budget; e.g. $10 at 85¢ = 11 contracts. Contracts pay $1 each if they win. The order book must show enough size for the whole order. One position and one entry per market. No re-entry after exit, including after restart.
- Exit at the executable bid below $0.40 after at least 1 second of continuous observed confirmation, or at entry + $0.10. A take-profit increment of 1 disables profit-taking and waits for resolution. A **take-profit price** (e.g. 0.99) sells as soon as the bid reaches that price, never below it; 0 turns it off. If both take-profits are set, the lower target applies. A stop price of 0 disables the stop. With both off, every position is held until the market settles ($1.00 or $0 per contract, no exit fee). A target above $1 cannot execute before resolution.
- Polling is every 2 seconds plus network latency; confirmation therefore occurs on the next eligible poll, not exactly at 1 second.
- Delta is fixed to 0 / disabled. Nonzero underlying-price delta filtering is not implemented.
- $1,000 initial simulated cash; $400 daily realized profit / $200 daily realized loss limits. Positive inputs; 0 disables a limit. Limits use the UTC day and realized paper P&L after estimated fees, not total exchange balance changes. They block entries, never position exits, and cannot guarantee a maximum loss.
- Entries require enough best-level size for the entire order (a decision rule, in every mode). Exits sell into as many order-book levels as needed (up to 10 levels): a stop takes any available bid, a take-profit never sells below its target. If the book cannot absorb the whole position, the filled part is recorded and the rest retries on the next refresh. With the paper realism settings off: no latency, entries fill at the best ask, and fractional contracts are possible (see *Execution & risk features*). No queue position or market-impact model in any mode. Fills use the displayed spread, not the midpoint.
- Fee estimate per fill: `ceil(0.07 * contracts * price * (1-price) * 100) / 100`. This is an explicit modeling assumption, not a verified market-specific fee schedule. Fees can differ by market or change. No settlement fee is modeled.
- After market close, hold the paper position pending an explicit `settled`/`finalized` YES/NO result. No guessed outcomes. Missing/unsupported outcomes remain pending.

**Pause** stops new entries; open positions continue to be managed. Closing the application stops all monitoring. Restart preserves cash, position, journal, and settings, but always pauses new entries. A feed failure retains the position, resets stop confirmation, and blocks fills until a fresh snapshot is available. The dashboard shows stale data explicitly. Paper equity uses the last observed bid and does not deduct an estimated exit fee until a simulated exit.

Do not interpret paper results as live trading validation or profitability evidence. Live mode uses the same 2-second polling, so live fills can differ from paper fills (latency, queue position, fees). Shard transfers, backtesting, WebSockets and a packaged native desktop shell are not implemented.

## Reports and automated checks

- `pnlreport.py --mode shadow|paper|live [--period all|today|24h|7d|mtd|YYYY-MM-DD:YYYY-MM-DD] [--out FILE]`: read-only performance report. It covers win rate, realized P&L, expectancy, profit factor, equity, max drawdown, and breakdowns by entry price, time left, coin, side and exit. It also compares the ledger against paper/shadow on the same markets, including missed fills, and runs data-quality checks.
- `auditcheck.py [--no-network] [--out FILE]`: read-only health and accuracy audit. It checks the app, feeds, settlement-index stream, paper cash reconciliation, every trade record, the last 40 settlements and the chart's 60-second average against Kalshi's official values, the audit-log hash chain, secret-file permissions and memory. Exit status 1 on any failure.
- Scheduled in the Claude desktop app (Scheduled section): **Kalshi Desk: daily audit + shadow P&L report** (07:00 daily) and **health watch** (every 4 hours). Both are strictly read-only and write reports to `output/reports/`.

## Files and checks

- `app.py`: loopback-only Flask server, public feed, credential storage, mode switching, log download.
- `engine.py`: market state, accounting, persistence, pending settings; calls the strategy and the executors.
- `strategy.py`: every entry/exit decision (shared by all modes). `settings.py`: defaults, validation, fees, sizing.
- `executors.py`: PAPER/SHADOW simulated executors and the LIVE_SMALL executor. `fills.py`: order-book fill model.
- `fairvalue.py`: fair-value model. `safeguards.py`: equity, kill-switch, settlement tracking. `simqueue.py`: latency queue and fill comparison. `report.py`: comparison report. `audit.py`: hash-chained audit log.
- `live.py`: signed Kalshi client and live order executor (IOC orders, position/balance checks).
- `static/`: responsive dashboard, Paper/Live switch, strategy editor, journal, connection panel.
- `data/trade_log.jsonl`: append-only audit log of orders and trades.
- `data/paper.sqlite3`: local paper state, excluded from Git. Keep a backup before resetting it.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The app only binds to `127.0.0.1:8765`. Do not expose Flask's development server to a network. Mutating routes require a per-process session token and enforce local origin/host checks. Reload the dashboard after restarting the server.

## API references

- https://docs.kalshi.com/getting_started/orderbook_responses — dollar fixed-point bid ladders; YES asks derive from NO bids and vice versa.
- https://docs.kalshi.com/getting_started/api_keys — RSA-PSS and Ed25519 request signing.
- https://docs.kalshi.com/api-reference/orders/create-order-v2 — V2 order creation: YES-leg `bid`/`ask`, fixed-point price, IOC, `reduce_only`, `fill_count` / `average_fill_price` / `average_fee_paid`.

The public API connection was tested separately from account authentication. Account verification requires your actual key pair; no private credentials were available in the supplied attachments.
# kalshi_bot
# kalshi_bot
