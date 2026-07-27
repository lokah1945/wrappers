# Proxy-Wrapper Infrastructure Audit — 2026-07-27 (~13:30 WIB)

Repo: `/root/wrapper` @ HEAD `21d0f79` (clean, in sync with `github/main`). READ-ONLY audit; nothing was modified or restarted.

---

## 1. Services & Ports

Units are **user-level systemd** (`systemctl --user`, under root's `systemd --user`, PID 709) — *not* system units. All 5 enabled + active, `Restart=always`, `RestartSec=3`, logs appended to per-wrapper files. Installed units are byte-identical to the repo copies (`<dir>/systemd/*.service`) — no unit drift.

| Unit | ExecStart | WorkingDirectory | Listening (ss) | .env LISTEN_PORT | Documented | Drift |
|---|---|---|---|---|---|---|
| wrapper-nvidia-python | `uvicorn src.main:app --host 127.0.0.1 --port 9101` | nvidia-python | 127.0.0.1:9101 | 9101 | 9101 | none |
| wrapper-nous | `python3 wrapper_nous.py` | nous | 127.0.0.1:9102 | 9102 | 9102 | none |
| wrapper-opencode | `uvicorn src.main:app --host 127.0.0.1 --port 9103` | opencode | 127.0.0.1:9103 | 9103 | 9103 | none |
| wrapper-blackbox | `uvicorn src.main:app --host 127.0.0.1 --port 9104` | blackbox | 127.0.0.1:9104 | **9108, HOST=0.0.0.0** | 9104 | **YES** |
| wrapper-model-registry | `python3 model-registry/service.py` | /root/wrapper | 127.0.0.1:9200 | 9200 (`MODEL_REGISTRY_PORT`) | 9200 | none (but see F1) |

Re-verified prior findings:
- **Blackbox 9108 drift CONFIRMED**: `blackbox/.env` = `LISTEN_HOST=0.0.0.0`, `LISTEN_PORT=9108`; live process gets `127.0.0.1:9104` only because the unit passes CLI args that override env. A manual `python3 src/main.py` run would bind **0.0.0.0:9108** (public). `.env.example` correctly says 9104.
- **`.deployed_commit` stale CONFIRMED**: contains `62307eb` ("final clean audit", ~07-25 era; ancestor of main) vs HEAD `21d0f79`. Nothing appears to maintain it.
- **Legacy port 9100 CONFIRMED**: `/root/.codex/config.openrouter-nemotron.toml:14` → `http://127.0.0.1:9100/v1` — nothing listens on 9100; that Codex profile is dead. Other tomls correctly use 9102.

`runtime/*.commit` markers are **git-tracked and unreliable**: on disk (mtime 12:48) they read nous=`92893e7`, opencode/blackbox/model-registry=`1ad8845`, nvidia=`92893e7` — exactly the committed blob content, contradicting live `/health` git_commit for 3 of 5 services. The ExecStartPre `git rev-parse HEAD >` writes get clobbered by subsequent git operations (checkout/pull restores tracked content), and a tracked file can never contain its own deploy commit. Live `/health.git_commit` is the only trustworthy signal.

## 2. Running Code vs Repo (HEAD `21d0f79`)

| Service | Running since | Self-reported commit | Latest commit touching its code | Verdict |
|---|---|---|---|---|
| nvidia-python | 07-27 11:30:17 | `92893e7` | **`68c1d47` (12:05) + common `54168ad`** | **STALE — running WITHOUT its own NameError fix** (fix committed 35 min after start; on disk, not live) |
| nous | 07-27 12:37:39 | `21d0f79` | `21d0f79` (05:13, pulled 12:37) | current ✅ |
| opencode | 07-27 12:37:39 | `21d0f79` | `21d0f79` | current ✅ |
| blackbox | 07-27 12:37:39 | `21d0f79` | `21d0f79` | current ✅ |
| model-registry | 07-27 02:55:15 | `1ad8845` | `model-registry/`: none newer; **`common/`: f8f3e1b (20:45), 54168ad (22:28)** newer | **STALE** — misses common-package fixes (incl. F-1 ModelRegistryClient fix); PYTHONPATH=/root/wrapper imports `common` |

Timeline (reflog): pull→`1ad8845` @02:55 (registry started), pull→`92893e7` @11:22 (nvidia restarted 11:30), `68c1d47` committed @12:05, pull→`21d0f79` @12:37 (nous/opencode/blackbox restarted 12:37). nvidia-python was never restarted after 12:05. No NameError in its log since 11:30 restart, but the running image (92893e7) lacks both the `sys.path` fix and the fallback — the crash path is still live code. **Report only; no restarts performed.**

## 3. Health & Logs

All 5 `/health` endpoints returned **HTTP 200** (3–7 ms):

| Endpoint | Status | Notes |
|---|---|---|
| :9101 nvidia | 200 | keys 6/6, 102 models cached; **model_registry client: failed_posts=87, consecutive_failures=87, circuit_open=false**; 24h window: 24 req, **avg latency 120 s, p95 633 s, p99 719 s** |
| :9102 nous | 200 | keys 5/5; **registry client: 22 consecutive failures, circuit_open=true** |
| :9103 opencode | 200 | keys 6/6; registry client failed_posts=1 |
| :9104 blackbox | 200 | keys 5/5; registry client failed_posts=1 |
| :9200 model-registry | 200 | **`providers_loaded: []`** — empty despite 4 wrappers posting |

**Root cause found — registry ingestion is fully broken (HIGH):** `registry.log` shows **2,504 × `POST /internal/observations|/internal/catalog → 503`**. `service.py:_require_internal` fails closed with 503 when `MODEL_REGISTRY_ADMIN_TOKEN` is unset. The token **is present in `model-registry/.env`**, but `wrapper-model-registry.service` is the **only unit with no `EnvironmentFile=` line** (both installed and in-repo), and `service.py` does not load dotenv. So the registry has never had its token → every wrapper observation/catalog write is rejected → `providers_loaded=[]`. Health "ok" is misleading; the control plane is receiving nothing.

Log summary (full files; journald has no user-journal, units log to files):
- `nvidia_py.log`: **318 ERROR** (dominant: `[proxy_openai] error: Server disconnected` — upstream instability), 8 WARN, **18 × NameError `sanitize_header_value`** (04:26–04:27, all pre-restart; none after 11:30).
- `wrapper_nous.log`: mostly 200s; **1 × 500** — unhandled `json.decoder.JSONDecodeError` in `chat_completions` (wrapper_nous.py:1130) → ASGI traceback; should be a 400.
- `opencode.log`: **1 × 500** — same `JSONDecodeError` pattern.
- `blackbox.log`: 0 errors/warnings.
- `registry.log`: 0 tracebacks, but the 2,504 × 503 wall above.

## 4. Git Hygiene

- `git status`: clean except untracked `blackbox/metrics-snapshot.json`, `opencode/metrics-snapshot.json` — runtime artifacts written by `{blackbox,opencode}/src/metrics.py` (saved_at = service start 12:37). **Yes, they should be gitignored** — no `.gitignore` rule matches them.
- `.gitignore` covers `.env`/`.env.*` (with `!.env.example`), `*.log`, `*.db*`, `__pycache__/`, caches ✅. Verified **no `.env`, `.db`, `.log` files tracked**. Issues: file is heavily duplicated (two full Python/env/IDE blocks), typo `netrics.db`, blanket `nvidia/` ignore at the bottom (legacy dir), and **`runtime/` is NOT ignored — the 5 runtime markers are tracked**, which is what breaks them (§1).
- `registry-state.db` (2.5 MB) sits in repo root — untracked/ignored, fine, but odd placement.
- Secret scan of HEAD tree (`nvapi-`, `sk-`, `sk-ant-`, `ghp_`, `AIza`, `xox*`): hits only in `nvidia-python/.env.example` (placeholder `nvapi-xxxxx…`) and `tests/test_agent_runtime_contracts.py` (dummy `…-test-key-…` values). **No real secrets committed.** (Values verified as placeholders; not reproduced here.)

## 5. Tests (static inspection only — not run)

`tests/`: 20 test files + `run_all_tests.sh`, `run_transparency_check.py`, `perf/` (bench + load sim). Heavy coverage of `common/` + model-registry (registry, state, sanitize, validation, profile store, security).

Per-wrapper coverage:
- **nvidia-python**: only wrapper with its own in-tree suite (`nvidia-python/tests/test_anthropic_tools_transparent.py`) + shared coverage.
- **nous / opencode / blackbox**: no per-wrapper test dirs, but covered by shared suites (`test_agent_runtime_contracts.py`, `test_anthropic_transparency_all.py`, `test_concurrency_contracts.py`, `test_transparent_model_contract.py`) which load each wrapper via `importlib`/`sys.path`. Blackbox is thinnest (6 files mention it vs 17 for nvidia).
- Spot-checked imported symbols (`post_nous`, `post_nous_with_retries`, `KeyPool`, `anthropic_to_openai/openai_to_anthropic/stream_openai_to_anthropic`, `AnthropicStreamState`, `_parse_dsml_from_text`) — **all still exist**; no stale imports of removed modules found.
- Caveats: `test_real_integration.py` + `test_sdk_compatibility_simulation.py` require live services on 9101–9104/9200 (env-dependent, not pure unit tests); `tests/__pycache__/` committed?—no, ignored; shared tests mutate `sys.path`/`os.environ`, making them order-sensitive.

## 6. Docs Drift

| Doc | Claim | Reality |
|---|---|---|
| README.md | All 4 wrappers "✅ Production **100/100**" | Contradicted by repo's own audit commit `fbe3b6a` ("85/100, NOT 100/100") and by live findings F1/F2. Stale marketing. |
| README.md ports | 9101–9104, 9200 | Match ✅ |
| wrappers.json | `nous.log_file: null` | Actual: `nous/wrapper_nous.log` (set by unit). Stale. |
| wrappers.json | ports/units/sources | Match ✅ (`updated: 2026-07-24` — predates 3 days of fixes) |
| WRAPPER_CONTRACT.md | invariant list, no ports | No drift found; note invariant "circuit/cooldown" behavior differs live (nvidia registry-client circuit never opened at 87 consecutive failures vs nous open at 22 — likely because nvidia runs pre-fix code) |
| MODEL_AVAILABILITY.md | per-wrapper `model-state.db` gitignored | Consistent (`*.db*` ignored) |
| install.sh header | ports 9101–9104 | Matches units ✅ |

## 7. install.sh + productions/*.sh (static review)

`install.sh` — generally solid (set -euo pipefail, portable unit rendering, health gating). Issues:
- Renders units with `sed "s#/root/wrapper#${PROJECT_DIR}#g"` — breaks if the path contains `#`; only `&` is escaped.
- `pip install -r requirements.txt` into system Python as root (no venv, no `--require-hashes`).
- Restart loop `exit 1` on first unhealthy wrapper → aborts mid-deploy, leaving remaining services on old code (exactly the F2 failure mode).
- Health capture `curl … > file 2>&1` mixes stderr into the JSON artifact. Minor.
- Does not fix the model-registry `EnvironmentFile` gap because the repo unit itself lacks it (F1 is a repo bug, not deploy drift).

`productions/run_production_audit.sh` — trivial exec shim, fine. `publish_production_report.sh` — well-guarded (dirty-tree refusal, report-path allowlist, staged-file check, secret regex, remote verification), but: pushes **directly to `main`** with no review; secret scan covers only the report file (comment claims "report and staged files"); `git push HEAD:main` will hard-fail on divergence (fails safe).

---

## Findings Table

| ID | Severity | Finding | Evidence | Recommendation |
|---|---|---|---|---|
| F1 | **HIGH** | Model-registry rejects **all** internal writes (503) — ingestion dead since 02:55 | 2,504 × 503 in registry.log; `providers_loaded: []`; `service.py:88` fails closed without `MODEL_REGISTRY_ADMIN_TOKEN`; unit has **no `EnvironmentFile=`** (only unit missing it) though token exists in `model-registry/.env` | Add `EnvironmentFile=-/root/wrapper/model-registry/.env` to the repo unit, reinstall + restart (operator action) |
| F2 | **HIGH** | nvidia-python runs stale `92893e7`, missing its own NameError fix `68c1d47` (committed 12:05, process started 11:30) | `/health.git_commit=92893e7`; 18 × `NameError: sanitize_header_value` pre-restart; fix touches `nvidia-python/src/main.py` | Restart wrapper-nvidia-python at next window (not done — read-only) |
| F3 | MEDIUM | model-registry runs `1ad8845`, missing later `common/` fixes (`f8f3e1b`, `54168ad`) | started 02:55 pre-pulls; PYTHONPATH=/root/wrapper imports `common` | Restart with F1 fix |
| F4 | MEDIUM | blackbox `.env` says `0.0.0.0:9108`; live is `127.0.0.1:9104` only via CLI override; manual run would expose publicly on 9108 | `.env` vs unit ExecStart; `src/main.py:1209` honors env when run directly | Correct `.env` to `127.0.0.1` / `9104` |
| F5 | MEDIUM | `runtime/*.commit` markers git-tracked → clobbered by git ops; 3/5 contradict live `/health` | disk = committed blobs (`1ad8845`/`92893e7`) vs live `21d0f79` | Gitignore `runtime/`, keep ExecStartPre write; trust `/health.git_commit` |
| F6 | MEDIUM | nous + opencode return 500 (ASGI traceback) on non-JSON request body — unhandled `JSONDecodeError` | `wrapper_nous.py:1130`; 1 occurrence each in logs | Catch and return 400 SDK-shaped error |
| F7 | MEDIUM | nvidia registry-client circuit never opens (87 consecutive failures, `circuit_open=false`) vs nous open at 22 | `/health` of 9101 vs 9102 | Verify fixed in HEAD `common` client; else file bug (may resolve with F2 restart) |
| F8 | LOW | `.deployed_commit` stale (`62307eb`, ~07-25) vs HEAD `21d0f79`; nothing maintains it | file mtime 07-25 03:05 | Delete or automate; redundant with runtime markers |
| F9 | LOW | Legacy port 9100 in `/root/.codex/config.openrouter-nemotron.toml` — dead endpoint | line 14; nothing listens on 9100 | Update or remove profile |
| F10 | LOW | `{blackbox,opencode}/metrics-snapshot.json` untracked runtime artifacts, not gitignored | git status; written by `src/metrics.py` | Add `metrics-snapshot.json` to `.gitignore` |
| F11 | LOW | `.gitignore` duplicated blocks, typo `netrics.db`, blanket `nvidia/` ignore | file inspection | Consolidate |
| F12 | LOW | README "100/100" for all wrappers & wrappers.json `nous.log_file: null` stale | vs `fbe3b6a` audit (85/100) and live F1/F2; unit sets `wrapper_nous.log` | Update docs |
| F13 | LOW | install.sh: system-wide pip as root; `#`-in-path sed hazard; abort-on-first-failure leaves partial deploys | static review | Harden incrementally |
| F14 | INFO | nvidia upstream instability: 318 × "Server disconnected"; 24h avg latency 120 s, p95 633 s | nvidia_py.log, `/health` window stats | Monitor upstream; consider timeout/retry tuning |
| F15 | INFO | No real secrets in HEAD tree (only placeholders in `.env.example` / test dummies); no `.env`/`.db`/`.log` tracked | git grep patterns (values redacted) | none |
| F16 | INFO | Test gap ranking: blackbox thinnest coverage; only nvidia-python has an in-tree suite; integration suites require live services | tests/ inspection | Add per-wrapper unit suites, esp. blackbox |

**Bottom line:** all 5 services are up and healthy at the HTTP level and ports match the documented map (with the known blackbox `.env` drift), but the model-registry control plane has been silently rejecting 100% of telemetry since its 02:55 start due to a missing `EnvironmentFile` in its unit (F1), and nvidia-python is serving pre-fix code with its own crash fix sitting unapplied on disk (F2). Both need a coordinated restart after the one-line unit fix — not performed per read-only mandate.