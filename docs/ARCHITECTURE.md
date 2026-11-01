# Architecture

```
          Kalshi REST (markets, order book, trades)          Kalshi WebSocket (signed, read-only)
                     │  every ~2 s per coin                     │  CF Benchmarks RTI 1/s + 5 Hz, 60-s avg, trades
                     ▼                                          ▼
   app.feed() ── snapshot(m) ──► Engine.tick(m) ◄── spot_fn / index_fn ── indexfeed.IndexFeed
                                     │                                   (pricefeed: Coinbase 1-s fallback, display + volatility)
              ┌──────────────────────┼───────────────────────────┐
              ▼                      ▼                           ▼
     strategy.decide_exit    strategy.decide_entry        finalminute (record only,
     (stop, take-profit,     (price band, window, caps,    background writer)
      fair value, reversal)   filters, session cap,
              │               worst-case loss guard)
              ▼                      ▼
        executors: SimExecutor('paper'), SimExecutor('shadow'), LiveSmallExecutor ── live.LiveExecutor ── Kalshi orders
              │
              ▼
   Engine state (positions, shadow_positions, trades, cash, halts) ── SQLite (data/paper.sqlite3)
   audit.TradeLog ── data/trade_log.jsonl (hash chain, secrets redacted)
   Flask routes ── static/ dashboard (compact cards, strategy lab, journal, connection)
```

## Key invariants
- One decision path for every mode; only `executors.py` differs (`tests/test_modes.py`).
- LIVE_SMALL: never selectable unless `LIVE_TRADING_ENABLED=true` in `.env`, credentials saved, a connection test
  within 15 minutes, entries paused, no open positions, typed confirmation. Always 1 contract, IOC, exits reduce-only.
- A live order's quote must be under 10 s old at send time, counted from the start of the update.
- Ambiguous exchange replies halt live trading until a human clears it; the halt survives restarts.
- The market is marked used and an in-flight marker persisted before a live order is sent (no double entry on crash).
- Restart always returns to PAPER with entries paused.
- Rules that compare a price with the target use the settlement index only, never the Coinbase fallback.
- Every new position records `cfg`, the label of the trading settings that opened it.

## Settlement model (verified against Kalshi contract text, 2026-10-31)
YES if the simple average of the 60 one-second CF Benchmarks RTI values before close is at least the 60-second
average before open (`floor_strike`); ties resolve YES. Kalshi rounds the final value (BTC: 2 decimals).
