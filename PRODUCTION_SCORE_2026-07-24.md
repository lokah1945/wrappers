# Production scorecard — wrapper monorepo

Date: 2026-07-24
Latest verified commit: `2ccccfb`
Scale: 0–100 per aspect; this is a readiness score, not a claim that production deployment has been completed.

## Score summary

| Aspect | Score | Status | Basis |
|---|---:|---|---|
| Transparent model identity | 95/100 | Strong | Inference model fallback removed; exact-model tests and call-plan checks exist. Wrapper-scoped explicit aliases remain; client/session alias scope is not yet the default. |
| Key rotation and retry contract | 90/100 | Strong | Native key pools remain the only retry dimension; cross-wrapper retry tests pass. Provider-level rate-limit policy still needs more live fixtures. |
| OpenAI/Anthropic/Responses compatibility | 84/100 | Good | Existing cross-wrapper transparency and stream/tool tests pass; central profile coverage is not yet complete for every live model. |
| Central model identity registry | 92/100 | Good | Shared contracts, local registry, persistent profiles, scoped aliases, provider manifests, and optional central service are implemented. |
| Model call plans | 78/100 | Good | Exact call plan and provider endpoint data exist; full per-model/region/auth/parameter profiles still need to be populated. |
| Capability accuracy | 68/100 | Partial | Generic catalog profiles are safe but conservative; authoritative per-model capability/limit manifests are not yet complete for all providers. |
| Error taxonomy/classification | 84/100 | Good | Shared classifier, provider manifests, account-scoped 404 handling, and tests exist. More provider-specific live error fixtures are needed. |
| Account/endpoint availability state | 84/100 | Good | Account/endpoint-scoped state, mixed-state protection, rotating NVIDIA verification, and persistent observations are implemented. |
| Security/authentication | 84/100 | Good with gaps | Registry internal writes fail closed, payloads are sanitized, and boundary validation exists. mTLS, rate limiting, secret rotation, and production network policy remain deployment work. |
| Data integrity and migrations | 88/100 | Good | Catalog validation, profile persistence, SQLite schema migration metadata, and scoped status are present. Distributed multi-process migration/backup testing remains. |
| Async/concurrency safety | 88/100 | Good with gaps | Central observation queue is bounded and model state writes are moved off the event loop. NVIDIA probes still require further shared-session/probe-budget hardening. |
| Resource management | 87/100 | Good with gaps | Central sessions and queues are bounded; per-probe session creation and some provider-side resources still need optimization. |
| Performance/scalability | 83/100 | Good with gaps | Hot-path model-state writes are offloaded and central sync is queued. Load testing with production-size catalogs and key pools is still required. |
| Observability | 82/100 | Good | Model registry health stats, model status endpoints, scoped observations, and exact model audit fields exist. Alerts and dashboards still need production wiring. |
| Test coverage | 94/100 | Strong | 68 tests pass, cross-wrapper transparency passes, compile/static/security checks pass. Live upstream and chaos tests are not available in the sandbox. |
| Documentation/contracts | 88/100 | Strong | Model availability contract, transparent execution contract, central service docs, manifests, and deployment unit exist. Runbook needs production-specific values. |
| Deployment/operations | 68/100 | Not production-complete | Systemd unit and installer integration exist, but production services were not restarted from this workspace and central registry rollout was not live-validated. |
| Live end-to-end provider validation | 45/100 | Not verified | No production credential/upstream execution was performed in this audit environment. Provider entitlement, quotas, regions, and live catalog behavior remain external validation items. |

## Weighted readiness score

**Overall source/readiness score: 87/100**

This score is intentionally below production approval because:

- production services have not been restarted from the current commit;
- live upstream tests were not performed;
- model profiles are still generic for models that do not yet have authoritative manifests;
- central registry HA, mTLS, rate limiting, backup, and alerting are not complete;
- NVIDIA probe/session budgeting needs another performance hardening pass.

## Hard gates

Regardless of the numeric score, the following are mandatory before production activation:

1. `model_substitution_total == 0`.
2. No inference fallback code exists.
3. All internal registry write endpoints have a non-empty token or mTLS policy.
4. Production `.env` files define registry URL/token consistently.
5. `/health`, `/ready`, `/metrics/model-status`, and registry `/health` pass after restart.
6. A mocked upstream recorder confirms the requested model is unchanged across every key rotation attempt.
7. A live account-specific 404 is reported as account-scoped, not globally retired.
8. A live success is recorded against the correct credential/account scope.
9. Backup and rollback procedures are tested.
10. Production rate/load test passes without event-loop blocking or unbounded observation queue growth.

## Score interpretation

- `90–100`: production-ready for the measured scope, with live deployment evidence.
- `80–89`: strong readiness; limited production validation or operational gaps remain.
- `70–79`: code is substantially hardened, but production rollout gates remain.
- `50–69`: partial implementation; production use should be restricted.
- `<50`: not production-ready.
