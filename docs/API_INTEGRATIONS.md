# API integrations and connector inventory

## Kalshi (verified 2026-10-31 against docs.kalshi.com via an external fetcher)
| Use | Endpoint | File |
|---|---|---|
| REST base (production, Kalshi's recommended host) | `https://external-api.kalshi.com/trade-api/v2` | `app.py`, `live.py`, `auditcheck.py` |
| WebSocket (production) | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `indexfeed.py` |
| Market discovery | `GET /markets?series_ticker=KX<COIN>15M&status=open` | `app.py` |
| Market details | `GET /markets/{ticker}` | `app.py` |
| Order book | `GET /markets/{ticker}/orderbook?depth=10` → `orderbook_fp.yes_dollars/no_dollars` | `app.py` |
| Trades backfill | `GET /markets/trades?ticker=` | `app.py` |
| 1-min candles | `GET /series/{series}/markets/{ticker}/candlesticks` | `app.py`, `quotes.py` |
| Balance / positions | `GET /portfolio/balance`, `GET /portfolio/positions?ticker=` | `live.py` |
| Orders (V2) | `POST /portfolio/events/orders` (IOC, `reduce_only` for exits, client order id) | `live.py` |
| Streams | `cfbenchmarks_value`, `cfbenchmarks_value_5hz`, `trade` | `indexfeed.py` |

A live fetch on 2026-10-31 confirmed the market-list and order-book field names the parser uses; the bot's
own parser produced the correct best bid/ask from that real payload.

**Not implemented:** a demo-environment switch (`external-api.demo.kalshi.co`). It requires changing `live.py`,
which project rules reserve for explicit owner approval. Pending.

## Other data sources
| Source | Use | Auth |
|---|---|---|
| Coinbase Exchange public API | 1-s ticker fallback, 1-min candles for the 1H chart and indicators | none |

## Claude Code connector inventory (this development session)
| Connector | Purpose | State |
|---|---|---|
| GitHub (MCP) | repository access for this repo only | working |
| Firecrawl | used once to verify Kalshi docs and payloads | working |
| Context7 | library documentation | available, not needed (no new libraries) |
| Others listed by the host (Slack, Datadog, Asana, Linear, …) | not used | several failed to connect (proxy 403); not required |
No MCP server was installed for the bot itself. The running bot uses no third-party tools; credentials stay on the Kali machine.
