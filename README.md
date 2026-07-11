# ILMA Wrappers (Mono-Repo)

> LLM provider wrappers for the ILMA / Hermes Agent platform.
> Each wrapper is a thin proxy that fronts a cloud LLM provider (OpenAI-compatible),
> adds load-balancing, retries, circuit breaking, and metrics on top.

---

## Repository Status (2026-07-01)

| Aspect | State |
|--------|-------|
| GitHub repo | `lokah1945/wrappers` (public) |
| Default branch | `main` |
| Active wrappers | `nvidia` (v8.2+) |
| Planned wrappers | `codex`, `claude-code`, `blackbox`, `opencode`, `cloudflare`, `antigravity` |
| Source SOT | `/root/wrapper/` on the production host (single mono-repo) |

> **Note on current layout.** As of 2026-07-01, the `nvidia` wrapper occupies the
> repository root. This preserves the upstream git history (12 commits, 10 unique
> after `subtree split` deduplication). Future wrappers will be added as siblings
> so the final layout becomes:
>
> ```
> wrappers/
> ├── nvidia/           ← already here at root (legacy)
> ├── codex/            ← planned
> ├── claude-code/      ← planned
> ├── blackbox/         ← planned
> └── ...
> ```
>
> A future migration will move `nvidia/` into its own subfolder while keeping
> the existing tag/branch history. Until that migration, treat the **root
> directory** as the `nvidia` wrapper.

---

## Active Wrapper: NVIDIA

`index.js` is the source of truth for the NVIDIA wrapper.

| Field        | Value |
|--------------|-------|
| Service name | `wrapper-nvidia.service` (systemd --user) |
| Default port | `9100` |
| Provider     | NVIDIA NIM (`https://integrate.api.nvidia.com/v1`) |
| Models       | 120+ free models via NVIDIA NIM catalog |

### Key features

- Adaptive backpressure (workload-aware queue sizing)
- Provider circuit breaker (5 fails / 60s → OPEN 120s, half-open recovery)
- Stream heartbeat (env-gated, default OFF, `STREAM_HEARTBEAT=true`, 5 s interval)
- Retry budget cap (15 s) + jittered exponential backoff
- Score-based key selector with model + provider penalty + fallback
- Minimal 4-class error taxonomy: `MODEL | KEY | PROVIDER | NETWORK`
- Two-way credential sync with MongoDB SOT

### Files of interest

```
src/
  index.js                 ← proxyOpenai() entry, retry loops, ANTI-SILENCE watchdog
  key_pool.js              ← score-based key selector + provider penalty
  metrics.js               ← Prometheus-style metrics
  error_taxonomy.js        ← 4-class error classification
  stream_heartbeat.js      ← SSE heartbeat emitter
wrapper-nvidia.service     ← systemd --user template
install.sh                 ← idempotent installer
.env.example               ← canonical config keys
AUDIT_REPORT_2026-06-30.md ← production-readiness audit
CHANGELOG.md               ← all patches since v8.2
README_AGENT.md            ← detailed agent runbook
```

### Quick start

```bash
# 1. Install
cd ~ && ./wrapper/nvidia/install.sh

# 2. Configure environment
cp .env.example .env
$EDITOR .env  # fill NVIDIA_API_KEY

# 3. Start (it auto-enables in systemd --user)
systemctl --user start wrapper-nvidia.service
systemctl --user status wrapper-nvidia.service

# 4. Smoke test
curl -fsS http://127.0.0.1:9100/health
```

---

## Production Release — 2026-07-12

**Phase:** production migration of the wrapper-nvidia fix (staging in `clone-nvidia/`, live in `nvidia/`).

### Root cause (found from Claude Code usage, model `z-ai/glm-5.2`)
OpenAI-compatible clients — **Hermes, OpenAI SDK, LiteLLM, OpenCode, Kilo Code, and
any `/chat/completions` caller** — send requests to paths **without the `/v1`
prefix** (e.g. `/chat/completions`). The wrapper fell through to `handleCatchAll`,
which forwarded the *unprefixed* path straight to NVIDIA
(`https://integrate.api.nvidia.com/chat/completions`) and got a **404 — breaking
every text model for those clients**. Claude Code (`/v1/messages`) was unaffected,
which is why the failure only surfaced for non-Anthropic agents. The production
metrics DB showed repeated `404` on `/chat/completions` for `z-ai/glm-5.2`.

### Fixes applied
1. **OpenAI path normalization** (`handleRequest`, `src/index.js`): well-known
   OpenAI endpoint stems (`/chat/completions`, `/embeddings`, `/models`,
   `/images/*`, `/ranking`, `/infer`, `/responses`, `/audio/*`, `/moderations`,
   …) are transparently rewritten to their `/v1/...` form so they hit the real
   handlers. `/v1`, `/v2`, `/api` (Ollama), `/metrics`, `/health`, etc. are left
   untouched.
2. **SSE keepalive heartbeat** for the OpenAI streaming path (`handleChatCompletions`)
   — emits a comment frame every `HEARTBEAT_INTERVAL_MS` so clients don't time out
   and drop the connection mid-response during upstream silence (reasoning models,
   large prefill). The Anthropic path already had this.
3. **Capacity / timeout tuning** (`.env`): `INFLIGHT_SOFT_CAP 50 → 100`,
   `HEADERS_TIMEOUT_MS 15s → 30s`, `PACING_MAX_WAIT → 120`.

### Validation (end-to-end, via curl)
- `z-ai/glm-5.2`: `/chat/completions` (no `/v1`) → **200** (was 404);
  `/v1/chat/completions` → 200; `/v1/messages` → 200; streaming + `tool_use`
  returns a clean `tool_use` block + `message_stop`.
- **Full sweep of 109 text I/O models**: `/chat/completions` (no `/v1`) now returns
  **identical** status to `/v1/chat/completions` for every model. Remaining
  non-200s are genuine NVIDIA upstream errors (model unavailable on this account,
  cold-start latency, or specialized params), never a wrapper-introduced 404.

### Layout in this repo
| Path | Role | Port | systemd unit |
|------|------|------|--------------|
| `nvidia/` | **production** (live) | 9100 | `wrapper-nvidia.service` |
| `clone-nvidia/` | staging / pre-prod validation | 9910 | `wrapper-nvidia-clone.service` |

> Backup directories (`nvidia_backup_*`, `nvidia.backup.*`) are **local-only**
> and intentionally excluded from this repository.

---

## Roadmap (planned siblings)

| Wrapper        | Port | Provider backing         | Status |
|----------------|------|--------------------------|--------|
| `nvidia`       | 9100 | NVIDIA NIM               | ✅ live |
| `codex`        | 9103 | OpenAI Codex             | 🟡 filesystem-only |
| `claude-code`  | 9102 | Anthropic Claude         | 🟡 filesystem-only |
| `blackbox`     | —    | BlackBox AI              | 🔴 stub |
| `opencode`     | —    | OpenCode local           | 🔴 stub |
| `cloudflare`   | —    | Cloudflare AI gateway    | 🔴 stub |
| `antigravity`  | —    | Antigravity              | 🔴 stub |

🟡 filesystem-only = source exists at `/root/wrapper/<name>/` but is **not
tracked by this repository yet**. They will be `.git subtree add`-ed one-by-one
as they reach production-ready status (each promotion gated by an audit report
similar to `nvidia/AUDIT_REPORT_2026-06-30.md`).

🔴 stub = directory exists with placeholder content only. Cleanup planned.

---

## Multi-account credentials (SOT)

This repository's `wrappers` are wired against the ILMA MongoDB SOT, where
multi-account credentials are stored canonically:

```js
{
  provider: "github",
  git_host: "github.com",
  accounts: {
    "smahud@gmail.com":   { api_token: "***SAN***", validated: true,  added: "2026-06-09" },
    "lokah2150@gmail.com":{ api_token: "***SAN***", validated: true,  added: "2026-07-01",
                            github_login: "lokah1945", github_user_id: 243200618 }
  },
  default_account: "lokah2150@gmail.com",
  schema_version: 3
}
```

> Repository push automation uses `default_account`. Token rotation policy:
> 90-day cycle, oldest = `smahud` (2026-06-09), next rotation 2026-09-29.

Tokens are loaded at runtime via `ilma_sot_credential_retrieval`. They are
**never** committed to this repository.

---

## Contributing

Each wrapper follows the same meta-template:

1. `src/index.js`    — proxy loop (one HTTP method per provider family)
2. `src/key_pool.js` — credential selector + health tracking
3. `src/metrics.js`  — Prometheus counter/gauge/histogram
4. `install.sh`      — idempotent systemd registration
5. `*.service`       — user systemd unit
6. `AUDIT_REPORT.md` — production-readiness gate
7. `CHANGELOG.md`    — patch-by-patch narrative

Promotion criteria before subtree-adding a new wrapper here:

- [ ] All three retest types pass (`test_*.py`, `test_*.js`, e2e)
- [ ] Audit report shows zero `R-CRITICAL` findings
- [ ] Service runs 24 hours continuous with zero unhandled crash
- [ ] MongoDB SOT has a validating credential for the backing provider

---

## License

Internal — Huda Choirul Anam / ILMA. Not for public redistribution.
