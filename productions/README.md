# Production validation artifacts

This directory contains operator-facing instructions and a safe validation runner
for a VPS deployment of the wrapper monorepo.

The runner is intentionally **non-destructive by default**:

- it does not install packages;
- it does not change `.env` files;
- it does not restart systemd services;
- it does not migrate/delete databases;
- it does not run inference unless `--smoke` is explicitly supplied;
- it never prints API keys or response bodies.

## Files

- `PRODUCTION_RUNBOOK.md` — staged VPS execution instructions and acceptance gates.
- `run_production_audit.sh` — shell entrypoint.
- `production_audit.py` — dependency-light audit/test runner.
- `reports/` — generated reports are written here by the VPS agent.

## Basic preflight

```bash
cd /root/wrapper
bash productions/run_production_audit.sh
```

## Run repository tests and endpoint checks

```bash
bash productions/run_production_audit.sh \
  --run-tests \
  --registry-url http://127.0.0.1:9200
```

## Run an explicit same-model smoke test

The smoke test is opt-in because it consumes provider quota. The model is never
changed by the runner.

```bash
export WRAPPER_API_KEY='the-local-wrapper-bearer-token'
bash productions/run_production_audit.sh \
  --run-smoke \
  --wrapper-url http://127.0.0.1:9101/v1 \
  --model 'moonshotai/kimi-k2.6' \
  --api-key-env WRAPPER_API_KEY
unset WRAPPER_API_KEY
```

## Run a bounded load test

Only run this after the smoke test passes and only with an approved model/quota.

```bash
export WRAPPER_API_KEY='the-local-wrapper-bearer-token'
bash productions/run_production_audit.sh \
  --run-load \
  --wrapper-url http://127.0.0.1:9101/v1 \
  --model 'moonshotai/kimi-k2.6' \
  --api-key-env WRAPPER_API_KEY \
  --requests 50 \
  --concurrency 5
unset WRAPPER_API_KEY
```

Reports are written to:

```text
productions/reports/production-audit-YYYYMMDD-HHMMSS.md
```

The report contains statuses, timing, commit information, and remediation
recommendations, but never secrets or response bodies.
