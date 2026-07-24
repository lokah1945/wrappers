# Production runbook — wrapper VPS validation

## Purpose

Use this runbook after syncing the repository to the VPS. It validates the
runtime, workflow, central model registry, exact-model transparency, key
rotation behavior, and bounded performance without silently changing models.

## Important contract

```text
The requested model is always the model under test.
Only the native credential/key pool may rotate.
No test may use or enable model fallback.
```

## 0. Safety and preparation

1. Work from the actual deployed repository directory.
2. Confirm the current Git commit before and after deployment.
3. Take a backup of wrapper `.env` files and all `*.db`/`*.db-wal` files using the
   organization's approved secret-safe backup process.
4. Do not print `.env` contents, API keys, OAuth tokens, Authorization headers,
   request bodies, or provider response bodies.
5. Run the default preflight first. Do not use `--run-smoke` or `--run-load`
   until the preflight report is clean.

```bash
cd /root/wrapper
bash productions/run_production_audit.sh
```

## 1. Repository and dependency gate

The agent should verify:

- repository is clean or changes are explicitly documented;
- expected branch and commit are deployed;
- all wrapper directories exist;
- `model-registry/` exists;
- `install.sh` is executable;
- Python imports succeed;
- full tests pass using the already-installed environment;
- no package installation happens implicitly.

If dependencies are missing, stop and report the missing package. Install only
through the approved deployment procedure, then rerun the audit.

## 2. Configuration gate

Check presence, not values, of:

```text
nvidia-python/.env
nous/.env
opencode/.env
blackbox/.env
model-registry/.env
```

Required production concepts:

- each wrapper has its native provider credential configuration;
- `MODEL_REGISTRY_URL` is either intentionally empty (local-only mode) or points
  to the central registry;
- `MODEL_REGISTRY_ADMIN_TOKEN` is set on the central registry and all wrappers
  that publish to it;
- `VERIFY_ON_BOOT` is explicitly configured;
- `FREE_ONLY` is explicit, not accidental;
- no deprecated `DEFAULT_MODEL`, `REASONING_MODEL`, or model fallback variable is
  used for inference routing.

## 3. Service and endpoint gate

Check all services:

```bash
systemctl is-active wrapper-model-registry.service
systemctl is-active wrapper-nvidia-python.service
systemctl is-active wrapper-nous.service
systemctl is-active wrapper-opencode.service
systemctl is-active wrapper-blackbox.service
```

Then check:

```text
GET /health
GET /ready
GET /v1/models
GET /metrics/model-status
```

For the registry also check:

```text
GET http://127.0.0.1:9200/health
```

A healthy process with zero configured keys is not a ready inference backend.

## 4. Catalog and model registry gate

For every provider:

1. Fetch `/v1/models` through the wrapper.
2. Confirm response has catalog source/staleness fields where supported.
3. Confirm no catalog outage causes an inference model substitution.
4. Confirm central registry profile revisions are visible.
5. Confirm account availability is not presented as global availability.
6. Confirm conflicting account states appear as `mixed` or are filtered to the
   active account scope.

Do not treat a public provider catalog as account entitlement.

## 5. Explicit same-model smoke gate

This gate is opt-in because it consumes provider quota.

Set the wrapper's local bearer token in a shell environment variable and run:

```bash
export WRAPPER_API_KEY='...'
bash productions/run_production_audit.sh \
  --run-smoke \
  --wrapper-url http://127.0.0.1:9101/v1 \
  --model 'provider/model-a' \
  --api-key-env WRAPPER_API_KEY
unset WRAPPER_API_KEY
```

The runner checks:

- HTTP success or a structured provider error;
- returned model identity when present;
- requested model is never replaced;
- no local fallback error is generated;
- `/v1/chat/completions` remains compatible.

Run the same model on each intended wrapper separately. Do not use a different
model as a substitute for a failing smoke test.

## 6. Key rotation gate

Key rotation is allowed; model substitution is not.

Use the existing wrapper key-pool metrics and logs. The expected evidence is:

```text
same model ID across every attempt
key label/fingerprint changes only when retry policy permits
no model candidate list appears
in-flight returns to zero after the request/stream
```

If a forced key failure fixture is available in staging, use it. Do not revoke
or mutate production keys as part of this runbook.

## 7. Error matrix gate

Use approved provider fixtures or controlled staging responses for:

```text
401/403 key or account error
404 account-specific not deployed
404 wrong route/model
410 explicit EOL
429 key-level
429 model/deployment-level
400/422 capability mismatch
408/timeout
5xx upstream transient
```

Expected invariant:

```text
all retries use the same model
account-specific state never becomes global retirement
capability mismatch never selects another model
```

## 8. Streaming and agent gate

Run the repository transparency checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/run_transparency_check.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

For a live staging model, verify:

- Chat stream ends with `[DONE]` where appropriate;
- Anthropic stream ends with `message_stop`;
- Responses stream sends `response.completed` before `[DONE]`;
- tool calls remain structured;
- no in-flight leak remains after client cancellation/EOF;
- `previous_response_id` tool loops continue correctly.

## 9. Bounded performance gate

Only run after smoke and error gates pass:

```bash
export WRAPPER_API_KEY='...'
bash productions/run_production_audit.sh \
  --run-load \
  --wrapper-url http://127.0.0.1:9101/v1 \
  --model 'provider/model-a' \
  --api-key-env WRAPPER_API_KEY \
  --requests 50 \
  --concurrency 5
unset WRAPPER_API_KEY
```

Record:

- success rate;
- p50/p95/p99 latency;
- TTFT for streams;
- queue depth/dropped observation count;
- in-flight before/after;
- key RPM and cooldowns;
- event-loop/CPU/memory behavior.

Increase load gradually. Stop if upstream quota, queue depth, error rate, or
latency degrades unexpectedly.

## 10. Persistence and restart gate

In a maintenance window:

1. Record model/profile/catalog status.
2. Restart one wrapper at a time.
3. Confirm last-known-good catalog remains available.
4. Confirm profile and scoped alias persistence.
5. Confirm central registry observations remain scoped.
6. Confirm no database migration warning appears.
7. Confirm in-flight counters recover to zero.

Never delete `model-state.db` or `registry-state.db` to make a test pass.

## 11. Final acceptance

Production can be marked ready only when:

- preflight is clean;
- all intended services are active;
- smoke tests pass for the real requested model;
- every retry preserves the model ID;
- error matrix passes;
- streaming/agent checks pass;
- bounded load test passes;
- persistence/restart check passes;
- generated report contains no BLOCKED or FAIL status;
- current Git commit is recorded;
- report is copied to `productions/reports/` and attached to the deployment record.

A missing live provider test is `BLOCKED`, not `PASS`.
