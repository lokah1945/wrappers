# Production validation artifacts

This directory contains operator-facing instructions and a safe validation runner
for a VPS deployment of the wrapper monorepo.

Current scope is `nvidia-python`, `nous`, `openrouter`, and `model-registry`.
BLACKBOX and the OpenCode provider are pending and frozen; do not inspect,
test, probe, edit, install or deploy them. Read root `AGENTS.md` and
`wrappers.json` first. Explicit selection cannot bypass pending status.

The unified dashboard is `http://127.0.0.1:9106/`: home shows global status,
and the top dropdown selects an active wrapper dashboard. Pending providers
are shown only from metadata and are never queried. Current inference ports
are NVIDIA 9101, Nous 9102, and OpenRouter 9103; ports 9104–9105 remain
reserved for frozen providers. Port 9100 is occupied by the model-fetcher MCP
service and preserved; 9106 is the unified dashboard slot. Model-registry
remains on 9200. Client configurations must use the allocation in `wrappers.json`.

NVIDIA, Nous and OpenRouter were verified on production endpoints with their
explicit tested models and three CLI clients on **2026-10-11**, including real
SDK tool loops and CLI read-tool tasks. See [client backend setup](CLIENT_BACKEND_SETUP.md)
for exact models, versions and configuration. Model-specific native Responses
and reasoning limits remain documented. Installer scope follows lifecycle
status; it does not certify every model or endpoint. The [2026-10-07 review](PRODUCTION_READINESS_2026-10-07.md)
and [NVIDIA/Nous rollout evidence](reports/nvidia-nous-production-rollout-20261007.json)
remain historical records.

The [2026-10-11 production review](PRODUCTION_READINESS_2026-10-11.md) tracks
the unified dashboard, current source, release gates and rollout.
**All eight current controlled gates pass**, with 954 unit tests on each Python
environment, acceptance 141/141 and soak 6,204/6,204. Final rollout passes
31/31 on the frozen db1a89e7 source. The deployed dashboard passes 7/7 fresh
browser checks, and real client qualification passes **27/27**: 18 smoke/SDK
checks and nine actual CLI read-tool tasks. **Production verification is
complete for the tested models and client versions**; [final active-service
checks](reports/active-production-final-verification-20261011.json) pass 14/14.
The review keeps first-round evidence separate
from the current source and deployment results.

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
- `publish_production_report.sh` — guarded commit/push/release verification.
- `reports/` — generated reports are written here by the VPS agent.

## Basic preflight

```bash
cd /root/wrapper
bash productions/run_production_audit.sh
```

## Run repository tests and endpoint checks

Run all release checks against isolated local mocks, including SDK parsing,
both compatibility layers, bounded soak and unit tests:

```bash
bash tests/run_production_gates.sh --output-dir /tmp/wrapper-release-evidence
```

This command uses temporary databases and fake upstream credentials, refuses
occupied test ports and preserves tracked historical audit JSON. The same
command runs in `.github/workflows/production-gates.yml`.

Check the deployed services separately without inference:

```bash
bash productions/run_production_audit.sh \
  --run-tests \
  --registry-url http://127.0.0.1:9200
```

The preflight checks the four active services, their individual deployed commit markers,
and each wrapper's `/ready` contract. A response with `ready: false` fails even
when HTTP is 200. Protected readiness endpoints need the local bearer token in
`WRAPPER_API_KEY` (or the environment variable selected by `--api-key-env`).
Use `--report-dir /tmp/wrapper-audit` to keep generated reports outside the repo.

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

Publish exactly one report only after the audit exits and the code checkout is clean:

```bash
bash productions/publish_production_report.sh \
  productions/reports/production-audit-YYYYMMDD-HHMMSS.md
```

The publisher refuses to run when `origin` is missing, non-report changes are
present, secret-like values appear in the report, or remote SHA verification
fails.

The report contains statuses, timing, commit information, and remediation
recommendations, but never secrets or response bodies.
