# Baseline performance (measured)

All figures come from reports the owner ran on the Kali machine against `data/paper.sqlite3`. PAPER and SHADOW are
simulations on the real order book, not live results. No live trades have been made.

## Original configuration (paper, all history to 2026-10-31 22:08 UTC)
| Metric | Value |
|---|---|
| Closed trades | 653 |
| Win rate | 77.3% |
| Net P&L | -$56.52 |
| Profit factor | 0.98 |
| Expectancy | -$0.09 per trade |
| Average win / loss | $5.61 / -$19.53 |
| Max drawdown | $727.12 (66.5%) |
| Losses from stop-loss exits | 104 trades, -$2,028.92 |

## By day (paper)
| UTC day | Trades | Won | Net | Settings |
|---|---|---|---|---|
| Oct 30 | 278 | 68% | -$374.17 | entries 60-95c, stops, long windows |
| Oct 31 | 428 | 86% | +$311.85 | tightened during the day |
| Nov 1 to 12:00 UTC | 206 | 90.8% | +$212.73 (+4.61c/contract) | 0.80-0.92, window 6, no stop/TP, cap 0 |

SHADOW (realistic fills: depth walking, whole contracts, latency), Nov 1 to 12:00 UTC: 168 trades, 91.7% won,
+$214.22, +5.96c per contract. Oct 31: 201 trades, +$164.04.

## Reconciliation (auditcheck, 2026-11-01)
Paper cash $1,150.33 equals the expected value; 1,265 trade records reconcile; 40 of 40 settlements match Kalshi;
audit-log hash chain intact; no secrets in logs.

## What this baseline does and does not show
- The tightened settings have been profitable in both ledgers for about 1.5 days. That is too short to establish an
  edge: run `model_eval.py --mode shadow` for confidence intervals.
- PAPER fills average ~0.9c per contract better than SHADOW; use SHADOW for decisions.
