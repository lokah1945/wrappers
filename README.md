# Wrappers

**API backends for OpenAI and Anthropic compatible clients, including Claude Code, Codex and OpenCode.**

This monorepo provides protocol proxies with multi-key rotation, pacing,
metrics and streaming. Compatibility is verified for the tested endpoints,
client versions, models and conversion features recorded in current reports.

---

## Production scope (2026-10-11)

`nvidia-python`, `nous`, `openrouter`, and `model-registry` are the active production scope. **BLACKBOX and the OpenCode provider wrapper are `pending` and frozen**: agents must not read, analyze, edit, test, probe or deploy them. Read [AGENTS.md](AGENTS.md) and [wrappers.json](wrappers.json) before any upgrade or patch. Installation, audit and test selection must reject pending providers, including explicit CLI selection. OpenCode as a client application remains supported by active backends.

Native Chat, Responses and Anthropic requests preserve their protocol fields
and SSE events when the selected upstream supports that wire API.
Cross-protocol requests use explicit converters with tested feature limits;
new features require new conversion coverage. `RESPONSES_UPSTREAM_MODE=auto`
tries native Responses first and permits the owner-approved Chat adapter only
for a confirmed unsupported native route. Credential retries retain the
requested model and release capacity on disconnect.

## Production review (2026-10-11)

See [the current production review](productions/PRODUCTION_READINESS_2026-10-11.md)
for the unified dashboard on port 9106, architecture, current release gates,
deployment evidence and model-specific limits. **All eight current controlled
gates pass**, including 954 unit tests on each Python environment, acceptance
141/141 and soak 6,204/6,204. Final rollout passes 31/31 on the frozen db1a89e7
source. The deployed dashboard passes 7/7 fresh browser checks.
Real client qualification passes **27/27**: 18 smoke/SDK checks and nine actual
CLI read-tool tasks. **Production verification is complete for the tested
models and client versions**, with final active-service checks passing 14/14.
Historical failed attempts
remain recorded in the review. The 2026-10-07 rollout verified NVIDIA, Nous and
OpenRouter on their production endpoints with explicit models and all three
CLI clients. [NVIDIA/Nous rollout evidence](productions/reports/nvidia-nous-production-rollout-20261007.json)
records the deployed working-tree source and service checks. Current release
gates and deployment verification are recorded separately from that dated
qualification.

See [client backend setup](productions/CLIENT_BACKEND_SETUP.md) for Claude Code,
Codex and OpenCode client settings. Current source tests and real-provider
results are tracked separately; a native route must also pass its client parser.

## Backend qualifications (2026-10-11)

### Current backend qualifications

The table reflects the current tested scope. Lifecycle `production` means
eligible for maintenance, not approval for every client, model or feature.
Current source gates and real-provider results are in the production review
and its linked reports; pending providers remain frozen.

| Wrapper | Status | Port | Upstream | Entry point |
|---------|--------|------|----------|-------------|
| **nvidia-python** | Production verified: Nemotron model, three CLI versions and SDK/read-tool loops on 2026-10-11 | 9101 | NVIDIA NIM | `src.main:app` |
| **nous** | Production verified: Laguna model, three CLI versions and SDK/read-tool loops on 2026-10-11 | 9102 | Nous Research | `src.main:app` |
| **opencode** | Pending — frozen | 9104 | OpenCode Zen | `src.main:app` |
| **blackbox** | Pending — frozen | 9105 | BLACKBOX AI | `src.main:app` |
| **openrouter** | Production verified: LFM model, three CLI versions and SDK/read-tool loops on 2026-10-11 | 9103 | OpenRouter | `src.main:app` |
| **model-registry** | Active dependency; no inference client surface | 9200 | internal | `service:app` |

The explicit qualified models are `nvidia/nemotron-3-super-120b-a12b` on
NVIDIA, `poolside/laguna-xs-2.1:free` on Nous, and
`liquid/lfm-2.5-2.6b:free` on OpenRouter. Native Responses SDK accumulation,
function calls and full-history function results passed on the tested models.
All three CLI clients also completed actual file-read tool tasks on each
backend. Clients select these IDs explicitly; key retries never
change the requested model. The earlier `openai/gpt-oss-20b` and
`upstage/solar-mini4:free` native Responses defects remain documented
model-specific limits. [Client setup](productions/CLIENT_BACKEND_SETUP.md)
links the current qualification evidence.

### Archived verification (2026-08-01)

These counts are historical evidence,
not current release approval or authorization to rerun pending-provider tests.
- ✅ 241 unit + regression tests (incl. AI Gateway translation matrix)
- ✅ 63 streaming regression tests · 67 translation-matrix tests
- ✅ 445/445 live runtime E2E checks (5 wrappers × 3 surfaces × 22 modes)
- ✅ SDK adapter fixtures parsed with the official OpenAI SDK used by that audit; this did not verify every native provider or future client
- ✅ COMPATIBILITY_LAYER E2E — layer 2 (Anthropic upstream) + layer 3 (auto-discovery)
- ✅ 240/240 full-matrix audit checks (real anthropic + openai SDK clients)
- ✅ Soak ~20k requests, 0 failures

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
# Development example for the active NVIDIA wrapper
cd nvidia-python
uvicorn src.main:app --reload --port 9101

# Production (one worker per wrapper, behind the ingress)
uvicorn src.main:app --host 127.0.0.1 --port 9101 --workers 1
```

Use one worker per wrapper: key reservations, cooldowns and
`previous_response_id` histories live in process memory. Multiple workers
require an explicit design for shared state or routing affinity before they
can preserve conversation continuity.

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

The active wrappers implement these features:

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
- Shared reservation state protected by locks, with cancellation cleanup covered by regression gates
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

The active wrappers are verified against these requirements:

1. **Retryable credential errors** - A failed key does not end the request while another eligible credential is available
2. **All-key retry** - Try remaining unique eligible credentials for quota/rate-limit and invalid-key failures; invalid requests and model errors remain actionable
3. **Per-key cooldown** - Credential failures cool the key; model capacity can be scoped to the requested model
4. **Exact in-flight accounting** - Release each reservation on success, error and cancellation
5. **Native stream transparency** - Preserve upstream SSE and terminators; do not manufacture successful native completion
6. **Structured cross-protocol tools** - Convert the supported tool dialects and preserve native extensions
7. **Conversation continuity** - Keep tenant-scoped adapter history and reject unavailable context; native history remains upstream-owned
8. **Transparent model choice** - Keep the exact requested model during retries
9. **Actionable errors** - Preserve native upstream errors and shape translated/local errors for the selected client surface
10. **Explicit compatibility** - Record tested models, endpoints, clients and conversion limits

---

## 🏭 Production Deployment

### Quick Start

```bash
# 1. Clone repository
git clone https://github.com/lokah1945/wrappers.git
cd wrappers

# 2. Setup an active wrapper (NVIDIA example)
cp nvidia-python/.env.example nvidia-python/.env
# Edit .env with your API keys

# 3. Run wrapper (from inside the wrapper directory)
cd nvidia-python
uvicorn src.main:app --host 127.0.0.1 --port 9101 --workers 1

# 4. Access the wrapper's local dashboard
# Visit http://127.0.0.1:9101/dashboard in your browser
# The unified dashboard, installed separately, is http://127.0.0.1:9106/
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
├── opencode/                        # OpenCode Zen proxy (Port 9104)
│   ├── __init__.py
│   ├── README.md
│   ├── .env.example
│   ├── dashboard.html
│   └── src/
│       ├── __init__.py
│       └── main.py
│
├── blackbox/                        # BLACKBOX AI proxy (Port 9105)
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

**Audit:** 2026-08-01 · **241 unit tests · 445 E2E checks · 240 full-matrix checks · 0 failures**  
**Report:** [FULL_MATRIX_AUDIT_2026-08-01.md](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md)

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

The unified dashboard is at **`http://127.0.0.1:9106/`**. Its home page
shows the global status of active wrappers. A dropdown in the separate top
navigation selects an individual wrapper dashboard without covering its
existing controls. Pending providers appear only as frozen metadata;
their services, dashboards and logs are never queried.

Active wrappers retain their local **monitoring dashboard** at `/dashboard`:

- **Real-time metrics** - RPS, latency, error rate
- **Key status** - Available, blocked, in-flight
- **Model availability** - Per-model status
- **Rate-limit / 429 events** - Per-key cooldowns
- **Auto-refresh** - Every 10 seconds
- **Auth prompt** - Token entered client-side (not embedded in HTML)

The port plan is the existing model-fetcher MCP HTTP service **9100**,
NVIDIA **9101**, Nous **9102**, OpenRouter **9103**, reserved pending
providers **9104–9105**, and the unified dashboard **9106**. Port 9100 is
not reused because the unrelated model-fetcher MCP service already owns it; that service
is preserved. The internal model-registry remains on **9200**. Reserved
ports remain untouched while their providers are frozen. Client configurations
must use the migrated allocation in `wrappers.json`.

---

## 🧪 Testing

A wrapper is **not** contract-compliant until it passes the gates below
(WRAPPER_CONTRACT §11).

```bash
pip install -r tests/requirements.txt

# Current active-scope unit, runtime, SDK, conversion and bounded soak gates
PYTHON_BIN=/path/to/verified/venv/bin/python \
  bash tests/run_production_gates.sh --output-dir /tmp/wrapper-release-evidence
```

The gate selects production wrappers from lifecycle metadata, excludes pending
providers before reads or imports, and uses isolated state with mock upstreams.
It covers native unknown fields and SSE, Responses native-first policy,
continuation, cross-protocol fixtures, key rotation and cancellation. Record
current counts and exclusions from the generated report. Real-provider and CLI
smokes are separate: the explicit OpenRouter, NVIDIA/Nemotron and Nous/Laguna
models passed all three CLI clients, structured SDK tool roundtrips and actual
CLI read-tool tasks on their production endpoints on 2026-10-11. Native
Responses SDK accumulation was also verified in earlier staging. See [current evidence and deployment
status](productions/PRODUCTION_READINESS_2026-10-11.md). Earlier native defects
remain limited to the models recorded in the diagnosis; qualification does
not silently replace those models. Neither HTTP 200 nor a matching route
alone proves client compatibility.

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

### 2026-08-01 (Historical)
- ✅ **COMPATIBILITY_LAYER** — operator-declared upstream dialect (1=OpenAI, 2=Anthropic, 3=Auto) in every wrapper
- ✅ **SDK compatibility** — the audit's Responses adapter fixtures parsed with its official OpenAI SDK version
- ✅ **AI Gateway translation layer** — tested OpenAI↔Anthropic / Responses↔Chat fixture round trips (F1-F7); feature coverage remains bounded
- ✅ **Full matrix audit** — 240/240 checks with real SDK clients; contract §4/§10 enforcement
- ✅ 241 unit tests, 445 E2E checks, 0 soak failures

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

The active NVIDIA, Nous and OpenRouter wrappers expose OpenAI Chat, OpenAI
Responses and Anthropic Messages. Operators declare the upstream protocol;
matching protocols pass through and cross-protocol paths convert supported
features. Provider capability and client parsing still determine which model
can serve a given workflow. Pending provider wrappers are outside this scope.

```
# Active wrappers only
COMPATIBILITY_LAYER=1   # 1 = OpenAI Compatible (default), 2 = Anthropic Compatible,
                        # 3 = Auto Discovery (probe upstream once, cache, fall back to 1)
RESPONSES_UPSTREAM_MODE=auto  # Native first; adapter only for unsupported route
```

| Layer | Upstream speaks | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
|---|---|---|---|---|
| `1` (default) | OpenAI | native passthrough | native passthrough first; conditional Chat adapter | Anthropic↔OpenAI conversion |
| `2` | Anthropic | OpenAI↔Anthropic conversion | Responses→Chat→Anthropic conversion, then Responses output | native passthrough |
| `3` | discovered dialect | follows the cached dialect | follows the cached dialect and Responses policy | follows the cached dialect |

For an OpenAI upstream, `auto` permits the same-model Chat adapter only after
405, 501 or a definite unsupported-route 404. Generic 400, invalid model,
authentication, quota, timeout, 5xx and malformed successful native streams
remain errors. Unknown native `previous_response_id` cannot silently lose
history during fallback: the wrapper returns `previous_response_not_found`
and requests the full conversation. `native` forbids fallback; `chat` is an
explicit adapter mode for isolated legacy tests and must not force a supported
production endpoint through translation.

`COMPATIBILITY_LAYER=2` preserves Anthropic Messages, including new native
fields. Mode 1 converts Messages to OpenAI even when a provider also offers a
native Messages API; protocol selection follows the declared layer. Cross
conversion supports tested tools, text, images and reasoning features, not a
universal lossless mapping of every provider extension. Invalid configuration
values fail at startup. Full design: [`docs/COMPATIBILITY_LAYER.md`](docs/COMPATIBILITY_LAYER.md).

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

**Last Updated:** 2026-08-01  
**Version:** 3.1  
**Status:** Verified compatible (241 unit · 445 E2E · 240 matrix checks · 0 failures)  
**Repository:** https://github.com/lokah1945/wrappers
