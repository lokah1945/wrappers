# Model catalog and availability contract

## Purpose

All wrappers keep two different facts separate:

1. **Catalog** — the provider lists a model in its public or authenticated `/models` catalog.
2. **Account availability** — a particular credential/account can invoke that model on a particular endpoint.

A catalog entry is not proof of deployment or entitlement. An account-scoped failure is not proof of global retirement.

## State classification

| Provider result | State | Local hard block? |
|---|---|---:|
| 2xx | `available` | No |
| 404 containing `not found for account` / `Function ... for account` | `account_unavailable` | No |
| 404 route/page miss | `wrong_route` or `unknown` | No |
| 410 with explicit EOL/retired/sunset text | `globally_retired` | Yes, after provider evidence |
| 401/403 | `account_forbidden` | No; report auth/permission |
| 429 | `rate_limited` | No; cooldown only |
| timeout/408/5xx | `transient_failure` | No |
| 400/422 | `capability_mismatch` | No; fix request adaptation |

Background probes may record state and influence fallback selection, but an explicit model selected by a client must not be rejected solely because a background probe failed. The NVIDIA wrapper only uses the explicit global-retirement state for its default preflight hard block; `STRICT_BLOCK_UNAVAILABLE_MODELS` is intentionally opt-in.

## Persistence

Each wrapper has a local SQLite `model-state.db` (ignored by git) containing:

- `model_catalog`: last-good model metadata and TTL;
- `model_account_status`: state keyed by provider, credential fingerprint, model, and endpoint;
- `model_state_events`: audit history of state transitions.

Raw API keys are never stored. Only a truncated SHA-256 fingerprint is persisted.

The catalog uses stale-while-revalidate behavior. An upstream discovery outage must not erase the last-good snapshot. `MODEL_CATALOG_TTL_SEC` controls the freshness window and defaults to six hours; operators may choose a shorter or longer value.

## Regression requirements

Every provider adapter must test at least:

- catalog-listed model plus account-scoped 404 does not become global retirement;
- explicit EOL/410 is classified as retirement;
- 429 and 5xx are retry/cooldown states, not retirement;
- a later 2xx clears the account-scoped failure;
- state survives process restart;
- discovery can serve the last-good catalog when upstream is unavailable;
- error details remain diagnosable to the client without leaking credentials.
