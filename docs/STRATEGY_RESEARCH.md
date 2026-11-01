# Strategy research

The bot buys the favourite side of a 15-minute Up/Down market late in the session, at 80-92c, and holds to
settlement. The hypothesis is that late favourites settle more often than their price implies.

## Evidence so far (SHADOW and PAPER, Nov 1, settled trades)
| Price paid | Implied | SHADOW actual | PAPER actual |
|---|---|---|---|
| 80-84c | ~82% | 89.4% (94) | 87.8% (115) |
| 85-88c | ~86.5% | 92.3% (39) | 91.3% (46) |
| 89-92c | ~90.5% | 96.7% (30) | 97.7% (43) |
Every band won more often than its price implied. The samples are small: the 95% Wilson interval for the 80-84c band
in SHADOW (84 of 94) is roughly 81.5%-94.1%, so its lower end touches the implied 82%. `model_eval.py` reports these
intervals; the edge counts as established only when the interval clears the price.

## Directive strategies mapped to this project
| # | Strategy | Status |
|---|---|---|
| 1 | Momentum continuation | This is the live strategy (late favourite). Measured above. |
| 2 | Confirmed reversal | Reversal exit built (record-only first). Stop-loss reversals lost money (S1). |
| 3 | Volatility breakout | Bollinger filter built (off), entry-side only. Untested. |
| 4 | Mean reversion | Not built: buying the side behind with minutes left is long-shot buying; entries below 80c lost money. |
| 5 | Order-flow imbalance | Not built: needs the WebSocket order-book stream (O2). Trade prints are already recorded. |
| 6 | Settlement-aware forecasting | Final-minute monitor computes P(YES) from the published part of the settlement average. Collecting. |
| 7 | Probability mispricing | `model_eval.py` walk-forward model shows whether a model beats the market price out of sample. Fair-value model rejected (S4). |
| 8 | Selective ensemble | Not justified until two independent signals each show out-of-sample value. |
| 9 | Time-to-expiration | Measured: entries with 6-15 min left lost money; 0-6 min profitable (window 6). |
| 10 | Regime adaptation | Not built: needs weeks of data; hour-of-day breakdown exists in `sessionreport.py`. |

## Tests in flight (one change at a time; settings labels separate the periods)
1. Majority direction filter: against-majority trades won 80-85% vs 94% with the majority in four reports.
2. Session risk cap (risk control, after 1).
3. Bollinger entry filter.
4. Reversal exit and final-minute entries: decided from their record-only data.

## Rejected
Stop losses (S1), entries below 80c or above 92c, windows longer than 6 minutes, the fair-value model (S4), per-coin
sizing (coin rankings reverse day to day; only SOL was consistently bad).
