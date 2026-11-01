# Security audit (2026-11-01)

| Check | Result |
|---|---|
| Secrets in git (all history) | None: no `.env`, `.pem` or key files tracked; the only PEM strings are a formatter and a test dummy |
| `.gitignore` | `.env`, `*.pem`, `*.key`, `.private/`, `data/`, `output/`, `.venv/` ignored |
| Credential storage | `.private/credentials.json` written with mode 0600 inside a 0700 directory; plaintext protected by OS permissions (not encrypted) |
| Credentials in API/UI/logs | Never returned to the browser; audit log redacts credential-named fields and PEM-like strings (`audit.py`) |
| Network exposure | Flask binds 127.0.0.1:8765 only, debug off; mutating routes require a per-process token and local Origin/Host |
| Response headers | `Cache-Control: no-store`, `nosniff`, strict CSP (self only), `frame-ancestors 'none'` |
| Request signing | RSA-PSS / Ed25519 per Kalshi docs (`live.py`, `indexfeed.py`); signatures never logged |
| Live trading gate | Requires `LIVE_TRADING_ENABLED=true` in `.env` (new) plus existing checks |
| Dependencies | Flask >=3,<4 and cryptography >=41 only; no lock file (pin versions before live: `pip freeze > requirements.lock`) |
| Third-party tools | The running bot sends credentials only to Kalshi; no MCP server or external service receives them |

Recommendations: pin dependency versions; keep the Kali machine's disk encrypted; rotate the Kalshi API key if it
was ever pasted anywhere outside `.env` / the Connection page.
