# Model evaluation

## Tool
`model_eval.py` (read-only, standard library only, runs in the bot's virtualenv; about a minute on 1,000 trades):
```
.venv/bin/python model_eval.py --mode shadow --since 2026-11-01 --out output/reports/model-eval-$(date +%F).md
```
1. Net performance with 95% bootstrap intervals for per-contract P&L and profit factor, Wilson interval for win rate,
   fee drag and drawdown. States whether the edge is established (interval above 0) or not.
2. Market calibration: the price paid as a probability forecast vs. realised wins (Brier, log loss, ECE, per band).
3. Walk-forward L2 logistic regression on entry-time information (log-odds of price, minutes left, side, coin):
   5 chronological blocks, each scored by a model trained only on earlier trades (needs 250+ settled trades).
   Compared with the market price as a forecaster; reports out-of-sample results for trades the model rates positive-edge.
4. Loss attribution by price, minutes left, coin, side, exit type and hour.

Verified by `tests/test_model_eval.py`: hand-checked metrics; it detects a planted +7-point edge and does not claim
one when the edge is negative; changing later outcomes does not change earlier predictions (no look-ahead);
the database file is byte-identical after a run.

## Models considered and why most were not built
| Model | Decision |
|---|---|
| Logistic regression (calibrated, walk-forward) | Built (`model_eval.py`) |
| Fair-value lognormal model (`fairvalue.py`) | Existing; rejected for entries: under-priced favourites (S4) |
| Final-minute settlement model (`finalminute.py`) | Built; calibration reported by `finalminutereport.py` once data exists |
| LightGBM / XGBoost / Random Forest | Not built: cannot be installed here, and ~1,000 trades with 4-6 features do not justify them. Revisit at 5,000+ settled trades if logistic beats the market. |
| Deep learning | Not justified by data size |

## Results
Results require the owner's data; run the command above and record the output in this file. Do not trade on a
model unless it beats the market price out of sample (section 3) with a positive per-contract result.

## Historical backtesting
Kalshi does not provide historical order-book depth, so a faithful order-book backtest of past days is not possible
from public data. The project instead uses forward recording: SHADOW (real book, delayed, depth-walking fills),
settings labels on every trade, and the final-minute recorder. `gridreport.py` and `model_eval.py` are the
replay/evaluation tools over that record.
