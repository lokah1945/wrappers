# Deep audit and remediation report — model availability

Date: 2026-07-24
Scope: `nvidia-python`, `nous`, `opencode`, `blackbox`, shared contract/tests
Mode: source patch, regression test, security/static review; no production service restart performed in this workspace

## Original incident

Claude Code received a generic model-not-found message for `moonshotai/kimi-k2.6` through `wrapper-nvidia`. The audit showed that the provider catalog listed the model while the NVIDIA credential used by the wrapper received an account-scoped 404 (`Function ... not found for account ...`). The old verify sweep placed every 404/410 model in a global `_retired_models` set, and the request gate rejected the model before upstream invocation.

## Root cause fixed

The wrapper now classifies provider outcomes with separate global and credential-scoped states. In particular:

- `Function ... not found for account` → `account_unavailable`;
- 404 route/page miss → `wrong_route`/`unknown`;
- 410 is global retirement only when the provider message contains explicit EOL/retired/sunset evidence;
- 401/403 → account permission/auth;
- 429 → rate limit/cooldown;
- timeout and 5xx → transient;
- 400/422 → capability/request mismatch.

Only explicit provider EOL is eligible for NVIDIA's default local hard block. Explicit concrete client-selected models are no longer rejected because a background probe reported an account-scoped or transient failure. The legacy strict setting also excludes account-scoped, rate-limit, timeout, capability, and unknown-transient states, so enabling it cannot recreate the Kimi false-retirement path.

The upstream NVIDIA account detail is preserved in normalized error responses, so Claude Code/SDK clients receive a diagnosable provider error rather than an opaque local retirement message.

## Model-state architecture added

`common/model_state.py` is a dependency-free shared implementation used by every wrapper. Each wrapper gets its own ignored SQLite `model-state.db` containing:

- `model_catalog`: last-good provider catalog with TTL;
- `model_account_status`: model state keyed by provider, credential fingerprint, model, and endpoint;
- `model_state_events`: state-transition audit history.

Raw API keys are never written. Only a truncated SHA-256 fingerprint is used for scope. Catalog refresh uses stale-while-revalidate, so a discovery outage does not erase the last-good snapshot.

Catalog refresh behavior:

- NVIDIA: existing active refresh plus persistent hydration on restart;
- Nous/OpenCode/Blackbox: persistent discovery cache plus independent daily background refresh;
- all wrappers expose `/metrics/model-status`;
- `MODEL_CATALOG_TTL_SEC` defaults to six hours;
- `MODEL_CATALOG_REFRESH_SEC` defaults to one day for Nous/OpenCode/Blackbox.

## NVIDIA-specific changes

- Account-scoped 404s no longer enter `_retired_models`.
- Probe results are persisted with state/reason/status and credential scope.
- `/v1/models` exposes `catalog_listed`, `availability_state`, `availability_scope`, `reason_code`, and `checked_at`.
- Runtime upstream failures are recorded without storing credentials.
- Non-retriable model/route/capability failures are not retried across identical keys; rate limits, transient failures, and account auth failures remain retryable.
- NVIDIA error normalization preserves account-specific detail.
- Fixed `registry.py` refresh TTL unit bug (`time.time()` seconds were compared against a millisecond-scaled TTL).

## Other wrapper changes

- Nous, OpenCode, and Blackbox now persist catalog snapshots and annotate discovery records with scoped availability state.
- Their upstream request pools record availability outcomes and avoid retrying non-retriable model/route/capability failures across identical keys.
- Existing free-only policy remains an explicit policy gate and is not treated as provider availability.
- Test harness path ordering and temporary-source loading were hardened so the cross-wrapper transparency checks are deterministic.

## Validation performed

- Python compile/import smoke tests for all four wrappers: pass.
- Full pytest suite: **39 passed**.
- Cross-wrapper transparency runner: pass (`NV A→O`, `NV O→A`, `NV STREAM`, `NOUS`, `OPENCODE`).
- Focused Anthropic transparency and concurrency suites: **14 passed**.
- `git diff --check`: pass.
- Ruff focused checks (`F,E9`) on modified runtime code: pass.
- Bandit high-severity scan of shared state code: pass.
- `pip-audit` on all four requirements files: no known vulnerabilities reported.
- Regression test explicitly verifies that an NVIDIA account-scoped Kimi 404 does not enter `_retired_models`.

## Operational note

This patch does not grant an NVIDIA account access to Kimi. If the configured NVIDIA account still returns `Function ... not found for account`, the wrapper will now report that account-scoped condition accurately and will not call it globally retired. A credential/account with the appropriate NVIDIA deployment or entitlement is still required for a successful 200 response.

No production service was restarted by this audit. Deployment requires syncing the repository, installing dependencies if needed, restarting the relevant systemd units, and validating `/health`, `/v1/models`, `/metrics/model-status`, and a real request with the production credential.
