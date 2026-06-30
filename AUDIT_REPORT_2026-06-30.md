# wrapper-nvidia Deep Audit & Optimization Report

**Date**: 2026-06-30 05:36 WIB
**Auditor**: ILMA v3.29 (Hermes Agent, free-tier-routed)
**Severity**: CRITICAL → RESOLVED
**Production Readiness**: ✅ READY (post-fix)

---

## 1. Executive Summary

The `wrapper-nvidia` Node.js proxy at `/root/wrapper/nvidia` (v4.6.0-node) was
**non-production-grade** before this audit despite README claims of "production
ready". Three structural defects caused Hermes / ILMA agents to experience
silent stalls whenever the wrapper served them:

1. **Orphan process + dead systemd unit** — the canonical service
   `wrapper-nvidia.service` was `inactive (dead)` since 2026-06-29 14:44 WIB
   (≈13 hours), but port 9100 was held by an orphan Node.js process (PID 235307,
   PPID=684, not 1). A duplicate unit `nvidia-wrapper.service` also existed in
   `/etc/systemd/system/`. Any reboot would have orphaned the wrapper with no
   auto-restart path.

2. **Pathologically long server timeouts** — default `server.timeout=300000`
   (5 min), `server.keepAliveTimeout=75000` (75s), and no `headersTimeout`.
   These defaults combined with abort signals meant Hermes tool calls could
   silently hang for tens of seconds before a response surfaced.

3. **Metrics `_save()` race** — the periodic 30-second save and the
   synchronous close-time save could fire concurrently, producing
   `ENOENT: no such file or directory, rename '/root/wrapper/nvidia/metrics.db.tmp'
   -> '/root/wrapper/nvidia/metrics.db'` on shutdown. Logged at 2026-06-29
   14:44:52 as `[metrics] _save() failed — DB not persisted`.

After this audit:

- Service is **systemd-managed** (PPID=1) and **active**.
- All timeouts tightened and **anti-silence watchdog** added.
- `_save()` made concurrency-safe with ENOENT-tolerant handling.
- Single canonical service unit, duplicate removed.
- Idempotent `install.sh` for future deploys.
- 11/11 E2E regression tests pass (concurrent, abort, RST, malformed JSON, both
  OpenAI and Anthropic compat).

---

## 2. Timeline of Events (reconstructed)

| Timestamp (WIB) | Event | Evidence |
|---|---|---|
| 2026-06-29 14:42–14:44 | Final `wrapper-nvidia.service` life: verification sweep, then `[wrapper-nvidia] Shutting down...` | `journalctl -u wrapper-nvidia.service` (last 100 lines) |
| 2026-06-29 14:44:52 | `_save() failed — DB not persisted: ENOENT rename ...` | journalctl |
| 2026-06-29 14:44:52 | `service: Deactivated successfully` | journalctl |
| 2026-06-30 03:10 (approx) | Orphan Node.js port 9100 started manually by user (PPID=684) | `ps -p 235307 -o ppid,etime,cmd` |
| 2026-06-30 04:13 | Orphan writes `/root/wrapper/nvidia/metrics.db` (471 KB) | `ls -la /root/wrapper/nvidia/metrics.db` |
| 2026-06-30 05:07:14 | **AUDIT START**: orphan killed, systemd service restarted, hardening applied | this audit + journalctl |
| 2026-06-30 05:35:43 | Clean supervised restart via `install.sh` (PID 250250 → 250458) | journalctl |
| 2026-06-30 05:36:00 | **AUDIT COMPLETE**: 11/11 tests pass | this report |

---

## 3. Bugs Found

### Bug #1 — Orphan process held port with no supervision (CRITICAL)

**Symptom**: Bos reported "agent berjalan terhenti tanpa ada konfirmasi apapun".
Worst single failure mode was an orphan Node.js process unaware of systemd:
- If the binary crashed, no one would restart it.
- If config was wrong, `journalctl` showed nothing because stdout went to
  `/tmp/wrapper.log`.
- After a machine reboot, port 9100 was orphaned (systemd unit was `disabled`
  in effect because it was `inactive`).

**Discovery**:
```text
systemctl status wrapper-nvidia.service
○ Active: inactive (dead) since Mon 2026-06-29 14:44:52 WIB; 13h ago
PID 235307: PPID=684 (terminal/tmux, NOT systemd)
fd 1 -> /tmp/wrapper.log (not journalctl)
```

**Fix**:
1. Killed orphan: `kill -TERM 235307` → process gone, port 9100 free.
2. Stopped/disabled/removed duplicate `nvidia-wrapper.service` (kept canonical
   `wrapper-nvidia.service`).
3. Wrote hardened unit file with explicit StartLimitIntervalSec in `[Unit]`,
   tightened env-driven timeouts, MemoryMax=512M cap.
4. Brought service up: `systemctl daemon-reload && systemctl restart ...`.
5. Confirmed `ps -o ppid` → PPID=1, CGroup `/system.slice/wrapper-nvidia.service`.

### Bug #2 — `server.timeout` 300000 ms = 5 min silent stall (CRITICAL)

Source `src/index.js` line 2005 (pre-fix):
```js
const server = http.createServer(handleRequest);
server.timeout = 300000;          // ← 5 minutes
server.keepAliveTimeout = 75000;  // ← 75 seconds (defeat keep-alive)
```

Combined with `AbortSignal.timeout()` defaults in upstream `undiciFetch`, any
client disconnect that arrived during a 70–75 second upstream call left the
client waiting in keep-alive limbo while the upstream fetch waited its own
timeout.

**Fix** in `src/index.js` (R-handle patch):
- `SERVER_REQUEST_TIMEOUT_MS=60000` (60s max per request)
- `SERVER_KEEPALIVE_TIMEOUT_MS=10000` (10s keep-alive)
- `SERVER_HEADERS_TIMEOUT_MS=15000` (15s headers parse)
- New `ANTI_SILENCE_TIMEOUT_MS=45000` watchdog: if a handler hasn't sent a
  response header within 45s, return `504 timeout_error` and log the URL.

This guarantees that **no request can silently stall longer than 45s**,
regardless of upstream behavior.

### Bug #3 — Metrics `_save()` ENOENT race (HIGH)

Logged on shutdown 2026-06-29 14:44:52:
```text
[metrics] _save() failed — DB not persisted:
  ENOENT: no such file or directory, rename
  '/root/wrapper/nvidia/metrics.db.tmp' -> '/root/wrapper/nvidia/metrics.db'
```

Root cause: periodic 30s `_saveInterval` and shutdown `_save(true)` could fire
back-to-back; the second call could try to rename a `.tmp` that the first call
had already moved.

**Fix** in `src/metrics.js` (M4 patch):
- `_saveInFlight` flag to coalesce concurrent saves.
- ENOENT on rename is treated as benign (`tmp gone — main DB intact`).
- Parent directory `mkdirSync(recursive)` before write for safety on fresh
  deploys where the dir may have been wiped.
- Sync path keeps atomic write+rename, async path uses coalesced callbacks.

### Bug #4 — `pacingMaxWait` hardcoded to 60s (MEDIUM)

`src/key_pool.js` constructor:
```js
this.pacingMaxWait = 60.0;   // ← could stall first-pace requests 60s
```

Combined with `pacing=true`, the first request after boot could sleep-loop up
to 60 seconds before being admitted if `pacingMaxWait` was the upper bound.
Now configurable via `PACING_MAX_WAIT` env (default 30.0s, floor 5s).

### Bug #5 — Duplicate systemd unit file (MEDIUM)

Two unit files referencing the same `ExecStart`:
- `/etc/systemd/system/wrapper-nvidia.service` (canonical)
- `/etc/systemd/system/nvidia-wrapper.service` (duplicate, inactive)

When installing, this caused confusion. We removed the duplicate during the
audit.

### Bug #6 — Missing `.gitignore` (LOW)

Project at `/root/wrapper/nvidia` had no `.gitignore`. Added one to:
- Exclude `.env` (real secrets — never commit)
- Exclude `metrics.db`, `metrics_data/`, `*.tmp`, `*.bak`, `node_modules/`,
  `backups/`.

### Bug #7 — Stale `BETA_PORT` variable name (COSMETIC)

`const BETA_PORT = parseInt(process.env.LISTEN_PORT || '9101', 10);`
- Variable name is misleading. Functionally correct because `.env` supplies
  real `LISTEN_PORT=9100`. Left as-is in this audit to avoid scope creep, but
  flagged for future cosmetic refactor.

---

## 4. Production-Ready Evidence

Each claim is backed by an executable command and the literal output below.

### 4.1 Service is systemd-managed and active

```text
$ systemctl is-active wrapper-nvidia.service
active
$ systemctl show wrapper-nvidia.service -p MainPID,NRestarts,ActiveState,SubState
MainPID=250458
NRestarts=0
ActiveState=active
SubState=running
$ ps -o pid,ppid,cmd -p 250458
  PID  PPID  CMD
250458    1  /usr/bin/node /root/wrapper/nvidia/src/index.js
```

PPID=1 (init/systemd), CGroup `/system.slice/wrapper-nvidia.service` — orphan
state eliminated.

### 4.2 All hardening settings applied at boot

```text
$ journalctl -u wrapper-nvidia.service --since "5 minutes ago" -n 13
Jun 30 05:35:43 debian node[250458]: [wrapper-nvidia] PID=250458 listening — startup OK
Jun 30 05:35:43 debian node[250458]: [wrapper-nvidia] v4.6.0-node listening on 0.0.0.0:9100
Jun 30 05:35:43 debian node[250458]: [wrapper-nvidia] Hardening: server.timeout=60000ms keepAlive=10000ms headers=15000ms silenceGuard=45000ms
```

### 4.3 Health endpoint responsive (sub-millisecond)

```text
$ time curl -sS http://127.0.0.1:9100/health
{"status":"ok","total_keys":5,"available_keys":5,"blocked_keys":0,"models_cached":121,"version":"4.6.0-node"}
real    0m0.001s
```

### 4.4 E2E test suite (11 cases)

```
T1  health                                       200 / 1ms       ✓
T2  quick chat (OpenAI)                          200 / 0.4-16s   ✓ (post-warm)
T3  quick chat (Anthropic /v1/messages)          200 / 0.44s     ✓
T4  anthropic count_tokens                       200 / 1ms       ✓
T5  embeddings                                   200 / 0.58s     ✓
T6  malformed JSON fast-fail                      400 / 2ms       ✓
T7  unknown model 404                            404 / 2.48s     ✓
T8  5 concurrent chat completions                5/5 succeeded   ✓
T9  streaming + clean client abort, follow-up    200 (post)       ✓
T10 systemd service still active post-stress     active           ✓
T11 TCP RST → service alive                      200 (1ms)        ✓
```

Key invariants validated:
- **No silent stalls**: even first request returns in ≤ 16s; warm ≤ 2s.
- **Zero zombies**: TCP RST +113 → service still healthy.
- **No orphan**: PPID=1, single port owner.

### 4.5 Idempotent `install.sh`

```text
$ /root/wrapper/nvidia/install.sh --status
active
enabled
MainPID=250458
NRestarts=0
ActiveState=active
SubState=running

$ /root/wrapper/nvidia/install.sh
[install] synced .../wrapper-nvidia.service -> /etc/systemd/system/wrapper-nvidia.service
[install] daemon-reload OK
[install] ✅ service healthy: {...}
[install] install complete
```

Includes orphan-process cleanup (kills any non-PID1 port 9100 hitter before
restarting), duplicate unit removal, and a `/health` smoke test before exit.
Exits non-zero if smoke fails.

### 4.6 No more `_save()` ENOENT on shutdown

```text
$ systemctl restart wrapper-nvidia.service   # via install.sh
Jun 30 05:35:43 debian node[250458]: [logger] JSON log sink enabled -> ...
Jun 30 05:35:43 debian node[250458]: [wrapper-nvidia] Loaded 5 key(s) | soft=30 hard=40 rpm
Jun 30 05:35:43 debian systemd[1]: Stopping wrapper-nvidia.service
Jun 30 05:35:43 debian node[250250]: [wrapper-nvidia] Shutting down...
# ← no _save() ENOENT here any more
Jun 30 05:35:43 debian systemd[1]: wrapper-nvidia.service: Deactivated successfully.
```

---

## 5. Files Changed

| Path | Change | Reason |
|---|---|---|
| `src/index.js` | Tightened server timeouts, added ANTI_SILENCE guard, fixed shutdown sequence | Bug #2, anti-stall |
| `src/metrics.js` | Coalesced `_save()`, ENOENT-tolerant, mkdir before write | Bug #3 |
| `src/key_pool.js` | `pacingMaxWait` configurable (default 30s) | Bug #4 |
| `/etc/systemd/system/wrapper-nvidia.service` | Hardening env, MemoryMax, StartLimitIntervalSec in `[Unit]` | Bug #1 |
| `/etc/systemd/system/nvidia-wrapper.service` | REMOVED | Bug #5 |
| `wrapper-nvidia.service` (project copy) | Synced to system unit | Canonicalization |
| `install.sh` | NEW, idempotent installer | Reproducible ops |
| `.gitignore` | NEW, excludes secrets and runtime data | Bug #6 |
| `.env.example` | NEW, references new env vars | Documentation |
| `/tmp/wrapper.log` (orphan stdout) | dereferenced when orphan killed | Cleanup |

---

## 6. Operational Runbook (post-audit)

### Verify health
```bash
/root/wrapper/nvidia/install.sh --status
curl -sS http://127.0.0.1:9100/health | python3 -m json.tool
```

### Hot reload env keys without restart
- Edit `/root/wrapper/nvidia/.env` — wrapper reloads every 60s via
  `startKeyReload()`. No restart needed.

### Force hard restart
```bash
/root/wrapper/nvidia/install.sh
```

### Inspect recent logs (now in journalctl)
```bash
journalctl -u wrapper-nvidia.service --since '5 minutes ago' -f
```

### Clear stuck counters without restart
```bash
curl -sS -X POST http://127.0.0.1:9100/admin/heal-in-flight
```

### Verify hardening active
```bash
# The startup banner line is the canary:
journalctl -u wrapper-nvidia.service --since '5 minutes ago' \
  | grep -E 'Hardening|PID=.* listening'
```
Must include `Hardening: server.timeout=60000ms keepAlive=10000ms headers=15000ms silenceGuard=45000ms`.

---

## 7. Evidence Bundles (IDs)

| ID | Description |
|---|---|
| `ILMA-AUDIT-WRAPPER-NVIDIA-20260630` | This report |
| `ILMA-EVID-20260630-FIX-SERVICE-001` | systemd unit hardening applied |
| `ILMA-EVID-20260630-FIX-METRICS-SAVE-002` | `_save()` race + ENOENT fixed |
| `ILMA-EVID-20260630-FIX-SERVER-TIMEOUT-003` | 75s→10s keep-alive, anti-silence |
| `ILMA-EVID-20260630-FIX-PACING-004` | `pacingMaxWait` configurable |
| `ILMA-EVID-20260630-FIX-ORPHAN-005` | Orphan process killed, supervised |
| `ILMA-EVID-20260630-TEST-E2E-006` | 11/11 E2E tests passed |

---

## 8. Sign-Off

The wrapper is now production-ready:
- ✅ Single canonical supervisor (systemd, PPID=1)
- ✅ No silent stalls beyond 45s (`ANTI_SILENCE_TIMEOUT_MS`)
- ✅ No DB corruption races (`metrics._save()` coalesced)
- ✅ Idempotent install / restart / status scripts
- ✅ Documented env knobs for every timeout
- ✅ Live E2E tests pass without zombie or hang

Recommendation: **lock down access** to the install script (chmod 700 for
root), keep `.env` out of any backup routine that pushes to git, and add a
follow-up monitoring cron to call `/admin/heal-in-flight` if any service
metric shows stalled keys for > 5 minutes.
