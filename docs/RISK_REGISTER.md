# Risk register

| Risk | Likelihood | Impact | Controls in place | Residual / next step |
|---|---|---|---|---|
| Edge is luck (small sample) | Medium | High | SHADOW ledger; settings labels; `model_eval.py` intervals | Need 1-2 weeks; act only when the per-contract interval is above 0 |
| Correlated session loss (all coins reverse together) | Medium | High | daily loss limit, worst-case loss guard, kill-switch, `max_session_risk` (off) | Turn on the session cap (e.g. $80) after the majority-filter test |
| Asymmetric payoff (one loss ≈ 4-13 wins) | Certain | Medium | entry max 0.92, sizing | Inherent; judge by totals, not single trades |
| Live fills worse than SHADOW | Medium | Medium | IOC, 10-s quote age, SHADOW comparison | First live week at 1-3 contracts |
| Thin books on alt coins | Medium | Medium | SHADOW depth fills | Watch SHADOW partial fills before increasing size |
| Stale or missing index data | Low | High | index-only rules, freshness checks, gaps never confirm | Alerts (O3) |
| Ambiguous order result | Low | High | in-flight marker, halt until operator clears, position verification | none |
| Accidental live trading | Low | High | `LIVE_TRADING_ENABLED` gate + credentials + connection test + typed confirmation; restart returns to PAPER | none |
| Machine sleeps / app stops | Medium | Medium | positions settle at Kalshi regardless; restart is paused | Always-on host or keep-awake |
| Credential exposure | Low | High | `.private/` 0700, files 0600, git-ignored, log redaction | see SECURITY_AUDIT |
| Model over-fitting | Medium | Medium | walk-forward only, one change at a time | never auto-promote |
