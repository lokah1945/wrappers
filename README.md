# Wrappers

**Production-grade API proxies for Claude Code, OpenAI SDK, Anthropic SDK, and OpenClaw.**

This monorepo contains hardened, SDK-compatible transparent proxies that add multi-key rotation, pacing, metrics, streaming reliability, and full OpenAI + Anthropic compatibility.

---

## 🏆 Current Status (2026-08-04)

### Verified by Executable Gates — not by score claims

| Wrapper | Status | Port | Upstream | Entry point |
|---------|--------|------|----------|-------------|
| **nvidia-python** | ✅ Verified | 9101 | NVIDIA NIM | `src.main:app` |
| **nous** | ✅ Verified | 9102 | Nous Research | `src.main:app` |
| **opencode** | ✅ Verified | 9103 | OpenCode Zen | `src.main:app` |
| **blackbox** | ✅ Verified | 9104 | BLACKBOX AI | `src.main:app` |
| **openrouter** | ✅ Verified | 9106 | OpenRouter | `src.main:app` |
| **model-registry** | ✅ Verified | 9200 | internal | `service:app` |

**Verification (all reproducible, 8 gates — see [Testing](#-testing)):**
- ✅ 310 unit + regression tests (incl. 63 streaming regressions, AI Gateway translation matrix)
- ✅ 990/990 live runtime E2E checks (5 wrappers × 3 surfaces × 22 streaming modes)
- ✅ SDK-compat gate — every wrapper's Responses output parses with the official openai SDK (Codex parser)
- ✅ COMPATIBILITY_LAYER E2E — layer 2 (Anthropic upstream) + layer 3 (auto-discovery)
- ✅ 240/240 full-matrix audit checks (real anthropic + openai SDK clients)
- ✅ 55/55 real-SDK agent-loop checks — tool_use ⇄ tool_result round trips, DSML recovery, replay, tenant isolation
- ✅ Multi-agent concurrency storm — 12 concurrent SDK agents × 5 wrappers, zero cross-talk, zero leaked in-flight reservations
- ✅ Soak — sustained load, 0 failures

---

## 📚 Documentation

**Quick Start:**
- **[WRAPPER_CONTRACT.md](WRAPPER_CONTRACT.md)** — technical standards and contract (v3.2)
- **[COMPATIBILITY_LAYER.md](docs/COMPATIBILITY_LAYER.md)** — operator-declared upstream dialect
- **[FULL_MATRIX_AUDIT_2026-08-01.md](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md)** — full matrix audit (240 checks)
- **[DOCUMENTATION_INDEX.md](docs/DOCUMENTATION_INDEX.md)** — complete documentation index

**Wrapper-Specific Documentation:**
- [nvidia-python/README.md](nvidia-python/README.md)
- [nous/README.md](nous/README.md)
- [opencode/README.md](opencode/README.md)
- [blackbox/README.md](blackbox/README.md)
- [openrouter/README.md](openrouter/README.md)

---

## 🎯 Standardized Structure (2026-07-28)

All wrappers follow the **identical directory structure**:

```
wrapper/
├── __init__.py              # Package marker
├── README.md                # Wrapper-specific documentation
├── .env.example             # Configuration template
├── dashboard.html           # Monitoring dashboard
├── src/
│   ├── __init__.py          # Source package marker
│   └── main.py              # Main FastAPI application
└── systemd/ (optional)      # Systemd service files
```

### Standardized Run Command

All wrappers use the same uvicorn pattern — wrapper dir on `PYTHONPATH`,
entry point `src.main:app` (WRAPPER_CONTRACT §1.1):

```bash
# Development (hot reload) — from inside the wrapper directory
cd <wrapper> && uvicorn src.main:app --reload --port XXXX

# Production — ONE worker process per wrapper instance
uvicorn src.main:app --host 0.0.0.0 --port XXXX --workers 1
```

> **Do NOT run `--workers 4`.** The key pool, rate limiter, response store
> (`/v1/responses` idempotency + `previous_response_id`), and usage counters
> all live **in this process's memory**. With 4 workers each request lands on
> a random worker, so `previous_response_id` misses (~75 % of the time with 4
> workers), rate limits are multiplied ×N, and key rotation state diverges.
> Scale horizontally instead: run N single-worker instances behind a sticky
> LB if you truly need more capacity.

### Path Reference Pattern

For files in `wrapper/src/main.py`:

```python
# Access wrapper root directory (for dashboard.html, .env, etc.)
Path(__file__).parent.parent
# Result: wrapper/

# Access repo root (for common/ package)
Path(__file__).parents[2]
# Result: /path/to/repo/
```

---

## 🚀 Enterprise Features

All 5 wrappers implement these **enterprise-grade features**:

### 1. Configuration Validation
- `validate_config()` function at startup
- Validates required environment variables
- Validates port range (1024-65535)
- Fails fast with clear error messages

### 2. Request Correlation
- UUID-based request correlation ID
- Extracted from `x-request-id` header or auto-generated
- Logged with every request for distributed tracing

### 3. Latency Tracking
- Middleware-based latency measurement
- `X-Process-Time` header in responses
- Structured logging with request_id and latency_ms

### 4. Graceful Shutdown
- In-flight request tracking
- Wait up to 30s for requests to drain
- Force shutdown with warning if timeout
- Proper resource cleanup

### 5. Proper Concurrency
- `asyncio.Lock()` for async contexts
- `threading.Lock()` for sync contexts
- No race conditions or deadlocks
- Cancellation-safe lock acquisition

---

## 🔧 Recent Improvements (2026-07-28)

### Structure Standardization
- ✅ All wrappers restructured to `wrapper/src/main.py` pattern
- ✅ Consistent `__init__.py` placement
- ✅ Standardized run commands

### Dashboard Bug Fix
- ✅ Fixed path references for `dashboard.html`
- ✅ Fixed path references for `model-state.db`
- ✅ Fixed path references for `.env` hot reload

### Enterprise Features
- ✅ Added latency tracking middleware to all wrappers
- ✅ Added config validation to all wrappers
- ✅ Added request correlation to all wrappers
- ✅ Added graceful shutdown to all wrappers

---

## 📊 Non-Negotiable Runtime Contract

Every wrapper preserves these invariants:

1. **Provider errors are not surfaced prematurely** - A single failed key/token is never a whole-wrapper failure
2. **All-key retry before client error** - Try every available credential before returning error
3. **Per-key cooldown** - Failed key is cooled down temporarily
4. **Exact in-flight accounting** - Keys reserved and released exactly once
5. **Stream lifecycle is terminally complete** - Proper termination events
6. **No unstructured tool leakage** - Structured tool calls only
7. **Conversation continuity** - Proper `previous_response_id` handling
8. **Transparent model choice** - No silent model substitution
9. **SDK-shaped errors** - Proper error formats per SDK
10. **Provider-specific behavior stays behind adapter** - Uniform client semantics

---

## 🏭 Production Deployment

### Quick Start

```bash
# 1. Clone repository
git clone https://github.com/lokah1945/wrappers.git
cd wrappers

# 2. Setup environment
cp wrapper/.env.example wrapper/.env
# Edit .env with your API keys

# 3. Run wrapper (from inside the wrapper directory)
cd <wrapper>
uvicorn src.main:app --host 127.0.0.1 --port XXXX --reload

# 4. Access dashboard
open http://localhost:XXXX/dashboard
```

### Production Deployment

See [productions/PRODUCTION_RUNBOOK.md](productions/PRODUCTION_RUNBOOK.md) for:
- Systemd service configuration
- Docker deployment
- Load balancing
- Monitoring setup
- Troubleshooting

---

## 📁 Repository Layout

```
wrappers/
├── README.md                        # This file
├── docs/                            # All non-production documentation
├── WRAPPER_CONTRACT.md              # Technical standards
├── wrappers.json                    # Wrapper metadata
├── .env.example                     # Root environment template
├── install.sh                       # Installation script
│
├── nvidia-python/                   # NVIDIA NIM proxy (Port 9101)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
├── nous/                            # Nous Research proxy (Port 9102)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
├── opencode/                        # OpenCode Zen proxy (Port 9103)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
├── blackbox/                        # BLACKBOX AI proxy (Port 9104)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
├── model-registry/                  # Central model intelligence (Port 9200)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   └── service.py
│
├── common/                          # Shared utilities
│   ├── __init__.py
│   ├── middleware.py
│   ├── model_state.py
│   ├── translations/
│   └── model/
│
├── productions/                     # Production deployment docs
│   ├── README.md
│   ├── PRODUCTION_RUNBOOK.md
│   └── reports/
│
└── docs/                            # Non-production documentation
    ├── DOCUMENTATION_INDEX.md
    ├── CROSS_WRAPPER_BUG_POLICY.md
    ├── TEMPLATE_WRAPPER.md
    ├── ADR.md
    ├── audits/                      # Historical + current audit reports
    ├── reports/                     # Readiness, benchmark, planning reports
    └── artifacts/                   # One-off scripts and raw test output
```

---

## 🔍 Audit & Compliance

### Latest Audit Results

**Audit:** 2026-08-04 · **310 unit tests · 990 E2E checks · 240 full-matrix checks · 55 agent-loop checks · concurrency storm · 0 failures**  
**Report:** [FULL_MATRIX_AUDIT_2026-08-01.md](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md) · continuous deep-audit reports under `audit_report/` (INDEX.md)

### Audit Reports

- **[FULL_MATRIX_AUDIT_2026-08-01.md](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md)** - Full matrix audit (240/240 checks, real SDK clients)
- **[TRANSLATION_LAYER_AUDIT_2026-08-01.md](docs/audits/TRANSLATION_LAYER_AUDIT_2026-08-01.md)** - AI Gateway translation layer (F1-F7)
- **[CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md](docs/audits/CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md)** - SDK-compat / Codex (CODEX-RESP-02)
- **[CODEX_RESP_REAUDIT_2026-08-01.md](docs/audits/CODEX_RESP_REAUDIT_2026-08-01.md)** - Codex reasoning-only fix (CODEX-RESP-01)
- **[docs/audits/](docs/audits/)** - All historical audit reports

### Security Features

- ✅ BEARER_TOKEN authentication
- ✅ Constant-time token comparison
- ✅ Header injection prevention
- ✅ Cross-tenant isolation
- ✅ CORS restricted to localhost
- ✅ Rate limiting (per-IP and per-key; RATE_LIMIT_RPM=0 disables per-IP)
- ✅ Multi-key rotation with 429 backoff (no circuit breaker — removed 2026-07-30)
- ✅ Request size limiting

---

## 📊 Dashboard

All wrappers include a **monitoring dashboard** at `/dashboard`:

- **Real-time metrics** - RPS, latency, error rate
- **Key status** - Available, blocked, in-flight
- **Model availability** - Per-model status
- **Rate-limit / 429 events** - Per-key cooldowns
- **Auto-refresh** - Every 10 seconds
- **Auth prompt** - Token entered client-side (not embedded in HTML)

---

## 🧪 Testing

A wrapper is **not** contract-compliant until it passes the gates below
(WRAPPER_CONTRACT §11).

```bash
pip install -r tests/requirements.txt

# 1. Unit + parity + regression suite (310 tests, incl. translation matrix)
python -m pytest tests -q

# 2. Live agent-traffic E2E — real servers × mock upstream (990 checks)
python tests/e2e_runtime/run_runtime_e2e.py

# 3. SDK compatibility — Responses output must parse with the official
#    openai SDK (Codex parser), 5 wrappers × 4 modes
python tests/e2e_runtime/sdk_codex_compat.py

# 4. COMPATIBILITY_LAYER E2E — layer 2 (Anthropic upstream) + auto-discovery
python tests/e2e_runtime/compat_layer_e2e.py

# 5. Full matrix audit — real anthropic + openai SDK clients (240 checks)
python tests/e2e_runtime/full_matrix_audit.py

# 6. Sustained load — leak / pool-starvation / degradation
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# 7. Real-SDK agent loop — tool_use ⇄ tool_result round trips, DSML
#    recovery, streamed/non-streamed replay, tenant isolation (55 checks)
python tests/e2e_runtime/agent_loop_e2e.py

# 8. Multi-agent CONCURRENCY E2E — 12 concurrent SDK agents per wrapper,
#    zero cross-talk markers, zero leaked in-flight reservations
python tests/e2e_runtime/multiagent_concurrency_e2e.py
```

Quick smoke test after booting a wrapper:

```bash
curl http://localhost:XXXX/health
curl http://localhost:XXXX/v1/chat/completions \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"model": "model-name", "messages": [{"role": "user", "content": "Hello"}]}'
```

---

## 📈 Version History

### 2026-08-04 (Current) — Contract v3.2
- ✅ **Deep-audit rounds 5–7** — stream-integrity, DSML tool-call recovery parity, cross-tenant store-key uniqueness (`new_response_id`), openrouter `/v1/responses` turn persistence + `/metrics` JSON + `/health` in-flight parity
- ✅ **Two new gates** — real-SDK agent loop (55 checks) + multi-agent concurrency storm (12 agents × 5 wrappers, zero cross-talk)
- ✅ 8/8 gates green — 310 unit · 990 E2E · 240 matrix · 55 agent-loop · soak, 0 failures
- 📄 Contract: [WRAPPER_CONTRACT.md §12](WRAPPER_CONTRACT.md) (v3.2 changelog)

### 2026-08-01 — Contract v3.1
- ✅ **COMPATIBILITY_LAYER** — operator-declared upstream dialect (1=OpenAI, 2=Anthropic, 3=Auto) in every wrapper
- ✅ **SDK compatibility** — every wrapper's Responses output parses with the official openai SDK (Codex)
- ✅ **AI Gateway translation layer** — lossless OpenAI↔Anthropic / Responses↔Chat round trips (F1-F7)
- ✅ **Full matrix audit** — 240/240 checks with real SDK clients; contract §4/§10 enforcement
- ✅ Unit tests, runtime E2E checks, 0 soak failures

### 2026-07-28
- ✅ Standardized wrapper structure
- ✅ Fixed dashboard path references
- ✅ Added enterprise features to all wrappers
- ✅ Added latency tracking middleware
- ✅ Added config validation
- ✅ Added request correlation
- ✅ Added graceful shutdown

### 2026-07-27
- ✅ Deep E2E audit completed
- ✅ Cross-wrapper consistency verified
- ✅ Security hardening completed
- ✅ Dashboard bug fixes

### 2026-07-26
- ✅ Initial deep audit
- ✅ Bit-level code review
- ✅ Performance optimization

### 2026-07-24 to 2026-07-25
- ✅ Initial implementation
- ✅ Production deployment
- ✅ VPS audit and remediation

---

## 📞 Support & Resources

### Documentation
- **[DOCUMENTATION_INDEX.md](docs/DOCUMENTATION_INDEX.md)** - Complete documentation index
- **[WRAPPER_CONTRACT.md](WRAPPER_CONTRACT.md)** - Technical standards
- **Wrapper-specific READMEs** - Per-wrapper documentation

### Audit Reports
- **[FULL_MATRIX_AUDIT_2026-08-01.md](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md)** - Latest audit (240 checks)
- **[docs/audits/](docs/audits/)** - Historical audits

### Production
- **[productions/PRODUCTION_RUNBOOK.md](productions/PRODUCTION_RUNBOOK.md)** - Production guide
- **[WRAPPER_STANDARDIZATION_REPORT.md](docs/reports/WRAPPER_STANDARDIZATION_REPORT.md)** - Structure standards

---

## 🌐 Upstream Compatibility Layer (COMPATIBILITY_LAYER)

Every wrapper speaks **all three client surfaces** (OpenAI Chat, OpenAI
Responses, Anthropic Messages) regardless of what protocol the **upstream**
speaks. The upstream dialect is **operator-declared**, never guessed:

```
# .env — same variable in every wrapper
COMPATIBILITY_LAYER=1   # 1 = OpenAI Compatible (default), 2 = Anthropic Compatible,
                        # 3 = Auto Discovery (probe upstream once, cache, fall back to 1)
```

| Layer | Upstream speaks | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
|---|---|---|---|---|
| `1` (default) | OpenAI | passthrough | Responses↔Chat translate | Anthropic↔OpenAI translate |
| `2` | Anthropic | OpenAI→Anthropic→OpenAI translate | Responses→Chat→Anthropic→back | **passthrough** |
| `3` | auto | probed once per base URL, cached (`COMPATIBILITY_PROBE_TTL_SEC`) | same | same |

Setting `COMPATIBILITY_LAYER=2` makes an Anthropic-native upstream work with
OpenAI SDK clients *and* Claude Code with **zero translation on the Anthropic
surface** — more precise than guessing. Invalid values fail fast at startup.
Full design: [`docs/COMPATIBILITY_LAYER.md`](docs/COMPATIBILITY_LAYER.md).

## 🎓 Key Design Decisions

1. **Multi-key rotation** - Distributes load and provides redundancy
2. **429 with Retry-After on exhaustion** - SDKs auto-retry with backoff (no circuit breaker — removed 2026-07-30 to prevent false-positive cascading failures)
3. **Streaming heartbeat** - Keeps connections alive during long generations
4. **Dynamic aliases** - Allows flexible model routing
5. **Graceful shutdown** - Prevents request drops
6. **Request correlation** - Enables distributed tracing
7. **Startup validation** - Fails fast on configuration errors
8. **Latency tracking** - Monitors performance
9. **Standardized structure** - Easy maintenance and upgrades
10. **Enterprise features** - Production-ready from day one

---

## 📝 License

Internal use only.

---

**Last Updated:** 2026-08-04  
**Version:** 3.2  
**Status:** Verified compatible (310 unit · 990 E2E · 240 matrix · 55 agent-loop · multi-agent storm · 8/8 gates · 0 failures)  
**Repository:** https://github.com/lokah1945/wrappers
