# Implementation plan

| # | Item | Depends on | Status |
|---|---|---|---|
| 1 | Settings labels, majority filter, session cap | – | Done (`b0ee434`) |
| 2 | Codex review fixes | – | Done (`6cf93fb`) |
| 3 | Live-trading operator gate, `.env.example`, evaluation engine, docs | – | Done (this round) |
| 4 | 1-2 weeks of SHADOW; majority filter test; then session cap; then Bollinger | data | In progress (owner) |
| 5 | Operator alerts (Telegram) for halt, kill-switch, feed loss, long pause | bot token from owner | Not started |
| 6 | Demo-environment switch (`KALSHI_ENV=demo`) | owner approval for `live.py` | Not started |
| 7 | WebSocket order-book stream for faster, fresher quotes | – | Not started |
| 8 | Pinned dependency lock file | owner's machine | Not started |
| 9 | Live sizing above 1 contract with `.env` cap | items 4-6, owner approval | Not started |
| 10 | Final-minute entry rule / reversal exit | their record-only data | Waiting for data |
