# VPS remediation sequence after production audit

This sequence addresses the reports generated on 2026-07-25. Execute from the
actual VPS checkout only after reviewing the local diff. Do not discard dirty
changes before saving a backup/diff.

## Findings from the reports

- deployed repository commit was `7cbda8a`;
- working tree was dirty;
- all systemd wrapper units were inactive while several ports responded;
- Blackbox port 9104 refused connections;
- repository tests failed because the test environment saw extra configured keys;
- exact-model smoke returned HTTP 404;
- the previous public `model-registry/.env.example` contained a token-like value.

The token-like value has been removed from GitHub. Rotate the actual VPS token
anyway because it may have been used or copied.

## Step 1 — preserve evidence and inspect the VPS

```bash
cd /root/wrapper
mkdir -p /root/wrapper-backups/$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=$(find /root/wrapper-backups -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
git status --short
 git diff --binary > "$BACKUP_DIR/working-tree.patch"
git diff --name-only > "$BACKUP_DIR/working-tree-files.txt"
```

Do not run `git reset --hard` until the diff has been reviewed and backed up.

## Step 2 — sync to the approved cloud commit

```bash
cd /root/wrapper
git remote get-url origin
git fetch origin main
git checkout main
git reset --hard origin/main
git clean -fd -- productions/reports

git status --short
git rev-parse HEAD
```

The working tree must be clean before continuing. If local changes are needed,
commit them to a review branch instead of running production from a dirty tree.

## Step 3 — rotate registry token and configure secrets

Create a new internal registry token outside Git. Then configure:

```text
model-registry/.env:
  MODEL_REGISTRY_ADMIN_TOKEN=<new-secret>

nvidia-python/.env:
  MODEL_REGISTRY_URL=http://127.0.0.1:9200
  MODEL_REGISTRY_ADMIN_TOKEN=<new-secret>

nous/.env:
  MODEL_REGISTRY_URL=http://127.0.0.1:9200
  MODEL_REGISTRY_ADMIN_TOKEN=<new-secret>

opencode/.env:
  MODEL_REGISTRY_URL=http://127.0.0.1:9200
  MODEL_REGISTRY_ADMIN_TOKEN=<new-secret>

blackbox/.env:
  MODEL_REGISTRY_URL=http://127.0.0.1:9200
  MODEL_REGISTRY_ADMIN_TOKEN=<new-secret>
```

Never put the real value in `.env.example`, Git, report files, shell history, or
command-line arguments.

## Step 4 — remove orphan/manual processes

Before restarting services, identify listeners:

```bash
ss -ltnp | grep -E ':(9101|9102|9103|9104|9200)\\b' || true
systemctl status wrapper-model-registry.service wrapper-nvidia-python.service \\
  wrapper-nous.service wrapper-opencode.service wrapper-blackbox.service --no-pager
```

For every listening PID, verify its command line, working directory, and owner.
Stop only the known stale/manual wrapper processes through the approved change
procedure. Do not kill unrelated processes based on port number alone.

## Step 5 — install and start the official units

```bash
cd /root/wrapper
./install.sh --no-restart
systemctl daemon-reload
systemctl enable wrapper-model-registry.service \\
  wrapper-nvidia-python.service wrapper-nous.service \\
  wrapper-opencode.service wrapper-blackbox.service
systemctl restart wrapper-model-registry.service
systemctl restart wrapper-nvidia-python.service
systemctl restart wrapper-nous.service
systemctl restart wrapper-opencode.service
systemctl restart wrapper-blackbox.service
```

Verify:

```bash
systemctl is-active wrapper-model-registry.service
systemctl is-active wrapper-nvidia-python.service
systemctl is-active wrapper-nous.service
systemctl is-active wrapper-opencode.service
systemctl is-active wrapper-blackbox.service
```

If any unit fails:

```bash
journalctl -u <unit> -n 100 --no-pager
```

Fix the service/configuration issue before running model smoke tests.

## Step 6 — isolate repository tests

Use the updated audit runner. It removes provider API key variables and registry
secrets from the unit-test subprocess:

```bash
cd /root/wrapper
bash productions/run_production_audit.sh \
  --run-tests \
  --required-wrapper model-registry \
  --required-wrapper nvidia \
  --required-wrapper nous \
  --required-wrapper opencode \
  --required-wrapper blackbox \
  --require-registry
```

A test failure is not ignorable. Preserve the report and fix the test isolation
or code before proceeding.

## Step 7 — exact-model smoke test

Use the actual model intended for the client. Do not use another model as a
substitute for a failing test.

```bash
export WRAPPER_API_KEY='<local-wrapper-bearer-token>'
bash productions/run_production_audit.sh \
  --run-smoke \
  --required-wrapper nvidia \
  --required-wrapper model-registry \
  --require-registry \
  --wrapper-url http://127.0.0.1:9101/v1 \
  --model '<exact-model-id>' \
  --api-key-env WRAPPER_API_KEY
unset WRAPPER_API_KEY
```

If HTTP 404 occurs, inspect the generated report's normalized `error_type` and
`error_code`, then inspect `/metrics/model-status`. Do not change the model to
make the test pass.

## Step 8 — publish the report safely

After the audit completes:

```bash
cd /root/wrapper
REPORT=$(ls -1t productions/reports/production-audit-*.md | head -1)
bash productions/publish_production_report.sh "$REPORT"
```

The publisher refuses to push when:

- `origin` is missing;
- code/config changes are dirty;
- the report contains secret-like values;
- more than the selected report is staged;
- remote SHA verification fails.

## Final pass criteria

The VPS remediation is complete only when the latest report shows:

```text
FAIL = 0
BLOCKED = 0 for required services
working tree = clean
all required systemd units = active
runtime commit = repository commit
Blackbox endpoint = HTTP 200
exact-model smoke = PASS
repository tests = PASS
cross-wrapper transparency = PASS
```
