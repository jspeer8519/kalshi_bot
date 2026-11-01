# Defect register

Severity: P1 = could lose money or trade on wrong data; P2 = wrong records/reports or degraded safety; P3 = display/quality.
Every fixed defect has a regression test that fails on the code before the fix.

| ID | Sev | Defect | Found by | Status | Commit | Test |
|---|---|---|---|---|---|---|
| D1 | P1 | Live quote-age check counted only time inside the executor; delay earlier in the update could let a >10 s quote through | Codex review 2 | Fixed | `6cf93fb` | `test_codex_review2.QuoteAgeTests` |
| D2 | P1 | Reversal confirmation accepted gaps in index data (2 samples 60 s apart; one 5 s-old sample) and could trigger a live sale | Codex review 2 | Fixed | `6cf93fb` | `test_codex_review2.ReversalCoverageTests` |
| D3 | P1 | Reversal and final-minute rules could compare a Coinbase fallback price with the CF-index target (fake crossing) | Internal audit | Fixed | `c58161f` | `test_reversal.IndexOnlyTests` |
| D4 | P1 | LIVE_SMALL could be enabled from the dashboard with no operator-level switch | Mission-control audit | Fixed: `LIVE_TRADING_ENABLED=true` in `.env` now required | this round | `test_live_gate` |
| D5 | P2 | Final-minute probability treated missing past seconds as future seconds | Codex review 2 | Fixed | `6cf93fb` | `test_codex_review2.FinalProbCoverageTests` |
| D6 | P2 | Final-minute outcome recorded as definitive from 55 of 60 seconds | Codex review 2 | Fixed | `6cf93fb` | `test_codex_review2.OutcomeTests` |
| D7 | P2 | A monitor exception inside `Engine.tick` would skip that update's decisions | Internal audit | Fixed (isolated, after decisions, background writes) | `c58161f`, `6cf93fb` | `test_finalminute.IsolationTests`, `MonitorOrderTests` |
| D8 | P2 | SHADOW-ledger entry decisions logged as `mode: paper` | Owner's log | Fixed | `8a0e6c0` | `test_dual.DecisionAuditModeTests` |
| D9 | P2 | `test_stale_exit_is_not_sent` never opened a position, so it did not test the live stale-exit guard | Test run | Fixed (test) | `693a0cb` | itself (mutation-checked) |
| D10 | P3 | Price chart forced the target into the LIVE/5M scale: flat line | Owner | Fixed | `8896d98` | `test_chart_scale` |
| D11 | P3 | `if(!vals.length)return null;` commented out by a missing line break (two chart functions) | Internal | Fixed | `8896d98` | `test_chart_scale` |
| D12 | P3 | Dashboard collapsed to one column under 1100 px; ~6,000 px of scrolling | Owner | Fixed | `c3cadb4` | rendered check |

## Strategy defects (configuration, not code)
| ID | Finding | Evidence | Action taken |
|---|---|---|---|
| S1 | Stop loss at 40c destroyed value | 104 stops, -$2,028.92 (paper, all history) | Owner set stop 0 |
| S2 | Entries below 80c and with >6 min left lost money | `gridreport` and `xtab` (see BASELINE_PERFORMANCE) | Owner set 0.80-0.92, window 6 |
| S3 | SOL lost in three consecutive reports (66-70% wins) | sessionreport, pnlreport | SOL removed |
| S4 | Fair-value model under-priced favourites, blocking winning trades | model ~81-85% vs realised 94% | Fair value off |

## Open items
| ID | Sev | Item | Blocker |
|---|---|---|---|
| O1 | P2 | No demo-environment switch (production endpoints hard-coded) | Needs `live.py` change: owner approval |
| O2 | P2 | Order books polled by REST every ~2 s; no WebSocket order-book stream | Development item; not started |
| O3 | P2 | No operator alerts (phone/Telegram) for halts, kill-switch, feed loss | Needs a bot token from the owner |
| O4 | P3 | Final-minute outcome uses the unrounded average; Kalshi rounds (BTC 2 dp) | Affects near-ties in a research report only |
| O5 | P3 | `test_api` cannot run in the audit sandbox (Flask unavailable) | Runs on the owner's machine |
