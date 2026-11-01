# Production readiness

| Gate | Status | Evidence / blocker |
|---|---|---|
| Engineering | **Pass** with notes | 272 of 273 tests pass in the audit sandbox (`test_api` needs Flask and was not run there); audit 0 failures; recovery tests for halts, restarts, in-flight orders |
| Research | **Not yet** | ~1.5 days of SHADOW at current settings; edge positive but intervals not yet clear of zero/implied price |
| Paper / SHADOW | **GO** | live market data, settlement reconciled 40/40, cash reconciles, monitoring and reports in place |
| Demo environment | **Not available** | demo endpoint switch pending owner approval (O1) |
| Live (small) | **NO-GO** | needs: 1-2 weeks SHADOW with `model_eval` per-contract interval above 0; alerts; pinned dependencies; owner authorisation and `LIVE_TRADING_ENABLED=true` |
| Live (scaled, >1 contract) | **NO-GO** | additionally: live sizing change (owner approval), a live week matching SHADOW within ~1c/contract |
