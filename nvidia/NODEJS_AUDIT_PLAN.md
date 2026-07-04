# Comprehensive Audit Plan: Node.js wrapper-nvidia vs Python (Deprecated)

**Date:** 2026-06-27  
**Target:** `/root/wrapper/nvidia` (Node.js v4.4.0) on VPS `172.16.102.11`  
**Reference:** `/root/wrapper/nvidia_python_backup_20260627` (Python v4.0, **DEPRECATED - READ ONLY**)

---

## Executive Summary

The Node.js implementation (`wrapper-nvidia` v4.4.0) is the **active production candidate** replacing the deprecated Python version. This audit plan ensures the Node.js version achieves **full feature parity** and **production readiness** before decommissioning the Python backup.

**Key Finding:** Node.js version has **critical bugs** (undefined `rawMutated` variable in image generation handler) and **missing features** compared to Python. It is **NOT yet production-ready** as a drop-in replacement.

---

## 1. Feature Parity Matrix

| Feature | Python (Deprecated) | Node.js (Current) | Status | Gap Severity |
|---------|---------------------|-------------------|--------|--------------|
| **Core Proxy** | | | | |
| `/v1/chat/completions` (streaming + non-stream) | ??? Full | ??? Full | ??? Parity | - |
| `/v1/messages` (Anthropic compat) | ??? Full | ??? Full | ??? Parity | - |
| `/v1/embeddings` | ??? Full | ??? Full | ??? Parity | - |
| `/v1/images/generations` | ??? Full | ??? **BUG: `rawMutated` undefined** | **CRITICAL** | P0 |
| `/v1/infer` (native GenAI) | ??? Full | ??? Same bug as above | **CRITICAL** | P0 |
| `/v1/ranking` (rerank) | ??? Full | ??? Full | ??? Parity | - |
| `/v1/audio/*` (ASR/TTS) | ??? Via catch-all | ??? Not implemented | **HIGH** | P1 |
| `/v1/video/*` | ??? Via catch-all | ??? Not implemented | **HIGH** | P1 |
| `/v1/retrieval/*` | ??? Via catch-all | ??? Not implemented | **HIGH** | P1 |
| **Model Management** | | | | |
| `/v1/models` (cached, filtered) | ??? Full | ??? Full | ??? Parity | - |
| `/v1/models/:id` (capability metadata) | ??? Full | ??? Full | ??? Parity | - |
| Hourly model refresh | ??? Background task | ??? Background task | ??? Parity | - |
| 24h model verification probes | ??? Background task | ??? Background task | ??? Parity | - |
| **Key Pool & Rate Limiting** | | | | |
| Two-tier (KEY + MODEL) rate limiting | ??? Full | ??? Full | ??? Parity | - |
| Corroboration-based 429 classification | ??? Full | ??? Full | ??? Parity | - |
| FIFO admission queue (per-key/sec) | ??? Full | ??? Full | ??? Parity | - |
| Internal pacing (latency not 429) | ??? Full | ??? Full | ??? Parity | - |
| Dynamic key hot-reload (60s) | ??? Background task | ??? Background task | ??? Parity | - |
| Learned per-(key,model) limits | ??? Full | ??? Full | ??? Parity | - |
| **Metrics & Observability** | | | | |
| SQLite metrics (requests, tokens, latency) | ??? Native sqlite3 | ??? sql.js (WASM) | ?????? Different impl | P1 |
| Prometheus `/metrics/prom` | ??? Full | ??? Full | ??? Parity | - |
| JSON metrics API (`/metrics/*`) | ??? Full | ??? Full | ??? Parity | - |
| Structured JSON logging (Loki/ELK) | ??? Optional sink | ??? Missing | **HIGH** | P1 |
| WAL checkpointing (auto) | ??? Every 50 writes | ??? Missing | **MEDIUM** | P2 |
| **Admin & Operations** | | | | |
| `/health`, `/stats` | ??? Full | ??? Full | ??? Parity | - |
| `/admin/heal-in-flight` | ??? Full | ??? Full | ??? Parity | - |
| `/metrics/reset` | ??? Full | ??? Full | ??? Parity | - |
| Graceful shutdown (SIGTERM/SIGINT) | ??? Full | ??? Full | ??? Parity | - |
| **Client Probe Endpoints** | | | | |
| `/version`, `/api/version` | ??? Local | ??? Missing | **MEDIUM** | P2 |
| `/api/tags` (Ollama) | ??? Local | ??? Missing | **MEDIUM** | P2 |
| `/api/v1/models`, `/models` | ??? Local | ??? Missing | **MEDIUM** | P2 |
| `/props`, `/v1/props` (llama.cpp) | ??? Local | ??? Missing | **MEDIUM** | P2 |
| `/api/show` (Ollama) | ??? Local | ??? Missing | **MEDIUM** | P2 |
| `/favicon.ico` | ??? 204 | ??? Missing | **LOW** | P3 |
| **Anthropic Compat** | | | | |
| `/v1/messages` (streaming + non-stream) | ??? Full | ??? Full | ??? Parity | - |
| `/v1/messages/count_tokens` | ??? Approximate | ??? Missing | **MEDIUM** | P2 |
| Tool use / function calling translation | ??? Full | ??? Full | ??? Parity | - |
| Vision (image) support | ??? Full | ??? Full | ??? Parity | - |
| **Configuration** | | | | |
| Multi-host routing (LLM/GenAI/NVCF) | ??? Full | ??? Full | ??? Parity | - |
| Proactive param dropping (`DROP_PARAMS`) | ??? Full | ??? Full | ??? Parity | - |
| Retired model catalog | ??? Full | ??? Full | ??? Parity | - |
| Capability metadata (`/v1/capabilities*`) | ??? Full | ??? Full | ??? Parity | - |

---

## 2. Critical Bugs in Node.js (P0 - Must Fix Before Production)

### 2.1 `rawMutated` Undefined in Image Generation Handler
**File:** `src/index.js` lines 687-845  
**Issue:** Variable `rawMutated` used at lines 745, 783, 791 but **never declared**  
**Impact:** `/v1/images/generations` and `/v1/infer` **will crash** on first request  
**Fix:** Add `const rawMutated = JSON.stringify(body);` after body mutation (line ~715)

### 2.2 Duplicate/Confused Code Block in Image Handler
**File:** `src/index.js` lines 715-735  
**Issue:** Nested `if (body.size) { if (modelId in RETIRED_MODELS) ... }` block appears to be **copy-paste artifact** from chat completions handler - contains vision conversion and param dropping logic that doesn't belong in image generation  
**Impact:** Logic confusion, potential wrong parameter handling  
**Fix:** Remove the erroneous nested block (lines 715-735)

### 2.3 Missing `rawMutated` Declaration
```javascript
// Current (broken):
body.width = Math.max(w, minDim);
body.height = Math.max(h, minDim);
// ... missing: const rawMutated = JSON.stringify(body);
const resp = await undiciFetch(`${targetBase}${targetPath}`, {
  body: rawMutated,  // ReferenceError!
  ...
});
```

---

## 3. Missing Features in Node.js (P1 - High Priority)

### 3.1 Client Probe Endpoints (Local Responses)
Python implements these as **local responses** (no upstream call, no RPM cost):
- `GET /version`, `/api/version` ??? `{"version": "wrapper-nvidia-4.1.0"}`
- `GET /api/tags` ??? Ollama-compatible model list
- `GET /api/v1/models`, `/models` ??? OpenAI-format cached catalog
- `GET /props`, `/v1/props` ??? llama.cpp minimal props
- `GET/POST /api/show` ??? Ollama model info
- `GET /favicon.ico` ??? 204 No Content

**Why critical:** Hermes, Ollama, llama.cpp clients probe these **before** sending requests. Missing them causes:
- Wasted upstream calls (burns RPM)
- Polluted metrics with "unknown" 404s
- Client compatibility failures

### 3.2 Structured JSON Logging (Loki/ELK Sink)
Python: Optional `WRAPPER_JSON_LOG=1` enables JSONL file sink with structured fields  
Node.js: Only `console.log` / `console.error` - no structured logging

### 3.3 `/v1/messages/count_tokens` (Anthropic)
Python: Returns approximate token count locally  
Node.js: **Missing entirely**

### 3.4 Audio/Video/Retrieval Endpoints
Python: Handled via catch-all proxy with multi-host routing  
Node.js: Only explicit handlers for chat, embeddings, images, ranking - **no catch-all**

---

## 4. Architecture Differences (P2 - Medium Priority)

### 4.1 Metrics Storage: `sql.js` (WASM) vs Native `sqlite3`
| Aspect | Python (sqlite3) | Node.js (sql.js) |
|--------|------------------|------------------|
| Performance | Native, fast | WASM overhead |
| WAL Support | Full (auto-checkpoint) | Manual export/import |
| Concurrency | Thread-local connections | Single-threaded DB |
| Durability | `PRAGMA synchronous=NORMAL` | Manual `fs.writeFileSync` every 30s |
| Schema Migration | `ALTER TABLE` at connect | Full schema check + reset |

**Risk:** `sql.js` exports entire DB to memory on every save (30s interval). With 471KB `metrics.db`, this is manageable now but **will degrade** under load.

### 4.2 HTTP Server: Raw `http` vs `FastAPI`/`uvicorn`
- Node.js: Manual routing, manual SSE streaming, manual body parsing
- Python: Framework handles routing, validation, streaming, OpenAPI

**Risk:** More surface area for bugs in Node.js (evidenced by `rawMutated` bug)

### 4.3 Background Tasks
Python: `asyncio.create_task` + `asyncio.to_thread` for metrics offload  
Node.js: `setInterval` + inline async - **no thread pool for blocking DB writes**

---

## 5. Configuration Parity Check

| Env Var | Python | Node.js | Status |
|---------|--------|---------|--------|
| `NVIDIA_API_KEY_*` | ??? | ??? | ??? |
| `SOFT_LIMIT_RPM` | ??? | ??? | ??? |
| `HARD_LIMIT_RPM` | ??? | ??? | ??? |
| `LISTEN_HOST` | ??? | ??? | ??? |
| `LISTEN_PORT` | ??? | ??? | ??? |
| `NVIDIA_BASE_URL` | ??? | ??? | ??? |
| `NVIDIA_GENAI_BASE_URL` | ??? | ??? Missing in .env | ?????? |
| `NVIDIA_NVCF_BASE_URL` | ??? | ??? Missing in .env | ?????? |
| `QUEUE_LIMIT` | ??? | ??? | ??? |
| `MAX_RETRIES` | ??? (5) | ??? Hardcoded `QUIET_RETRIED_429=3` | ?????? |
| `REQUEST_TIMEOUT` | ??? (600) | ??? Hardcoded 600000ms | ?????? |
| `DATA_RETAIN_DAYS` | ??? (30) | ??? Hardcoded 30 | ?????? |
| `MODELS_REFRESH_SECONDS` | ??? (3600) | ??? Hardcoded 600 | ?????? |
| `KEYS_RELOAD_SECONDS` | ??? (60) | ??? 60000ms | ??? |
| `MAX_CONNECTIONS` | ??? (200) | ??? Hardcoded 80 | ?????? |
| `ENABLE_PACING` | ??? (true) | ??? Hardcoded true | ?????? |
| `PACING_MAX_WAIT` | ??? (60) | ??? 60s | ??? |
| `MODELS_VERIFY_SECONDS` | ??? (86400) | ??? 24h interval | ??? |
| `VERIFY_CONCURRENCY` | ??? (3) | ??? Sequential | ?????? |
| `DROP_PARAMS` | ??? ("think") | ??? ("think") | ??? |
| `UPSTREAM_ROUTES` | ??? JSON | ??? Not implemented | ?????? |
| `WRAPPER_JSON_LOG` | ??? | ??? Missing | ?????? |
| `KEY_LEVEL_RPM_RATIO` | ??? (0.8) | ??? | ??? |
| `CORROBORATION_WINDOW_S` | ??? (60) | ??? | ??? |
| `MODEL_BLOCK_CAP` | ??? (10) | ??? | ??? |
| `KEY_BLOCK_CAP` | ??? (30) | ??? | ??? |

---

## 6. Test Coverage Gap

| Test Type | Python | Node.js |
|-----------|--------|---------|
| Unit tests | ??? None found | ??? `test/` directory **empty** |
| Integration tests | ??? None found | ??? `test/test.js` **missing** |
| E2E tests | `test_e2e.py` exists | `test_e2e.py` exists (same file?) |
| Load tests | ??? | ??? |

**Critical:** No automated test suite exists for either implementation. Node.js `package.json` declares `"test": "node test/test.js"` but file doesn't exist.

---

## 7. Comprehensive Audit Plan

### Phase 1: Critical Bug Fixes (P0) - **BLOCKER for Production**
- [ ] **Fix `rawMutated` undefined** in `src/index.js` image generation handler
- [ ] **Remove duplicate/confused code block** (lines 715-735) in image handler
- [ ] **Verify image generation works** end-to-end with Flux/SDXL models
- [ ] **Verify `/v1/infer` works** for native GenAI models

### Phase 2: Missing Client Probe Endpoints (P1) - **Required for Client Compatibility**
- [ ] Add `GET /version` and `GET /api/version`
- [ ] Add `GET /api/tags` (Ollama format)
- [ ] Add `GET /api/v1/models` and `GET /models` (OpenAI format)
- [ ] Add `GET /props` and `GET /v1/props` (llama.cpp)
- [ ] Add `GET/POST /api/show` (Ollama)
- [ ] Add `GET /favicon.ico` ??? 204
- [ ] **Verify:** All return local responses (no upstream call, no RPM cost)

### Phase 3: Missing Features (P1)
- [ ] Implement `/v1/messages/count_tokens` (Anthropic approximate)
- [ ] Add catch-all proxy handler for `/v1/audio/*`, `/v1/video/*`, `/v1/retrieval/*`, `/v1/genai/*`
- [ ] Implement structured JSON logging (optional `WRAPPER_JSON_LOG=1`)
- [ ] Add WAL checkpointing to `metrics.js` (periodic `PRAGMA wal_checkpoint(TRUNCATE)`)

### Phase 4: Configuration Parity (P2)
- [ ] Add missing env vars to `.env` and code:
  - `NVIDIA_GENAI_BASE_URL`
  - `NVIDIA_NVCF_BASE_URL`
  - `MAX_RETRIES`
  - `REQUEST_TIMEOUT`
  - `DATA_RETAIN_DAYS`
  - `MODELS_REFRESH_SECONDS`
  - `MAX_CONNECTIONS`
  - `ENABLE_PACING`
  - `VERIFY_CONCURRENCY`
  - `UPSTREAM_ROUTES` (JSON parsing)
  - `WRAPPER_JSON_LOG`
- [ ] Make all hardcoded values configurable via env

### Phase 5: Architecture Hardening (P2)
- [ ] Evaluate `sql.js` vs native `better-sqlite3` for metrics
- [ ] Add thread pool for blocking DB operations (Worker Threads)
- [ ] Implement concurrent model verification (Python: `VERIFY_CONCURRENCY=3`)
- [ ] Add request validation middleware (like FastAPI's Pydantic)

### Phase 6: Test Suite (P1-P2)
- [ ] Create `test/test.js` with unit tests for:
  - KeyPool: acquire/release, rate limiting, pacing, syncKeys
  - Metrics: recordRequest, summary, percentiles
  - Anthropic compat: request/response translation
  - Capabilities: classify, describe, buildCatalog
- [ ] Create integration tests for all endpoints
- [ ] Add E2E test script (can adapt `test_e2e.py`)

### Phase 7: Load & Chaos Testing (P1)
- [ ] Load test: 100 concurrent requests, measure latency, error rate
- [ ] Rate limit stress: exhaust all keys, verify 429 handling
- [ ] Key rotation test: add/remove keys via `.env` hot-reload
- [ ] Model verification test: mark model unavailable, verify hidden from `/v1/models`
- [ ] Crash recovery: kill process, verify metrics DB integrity on restart
- [ ] Memory leak test: run 24h, monitor RSS growth

### Phase 8: Documentation & Runbooks (P2)
- [ ] Update `README.md` with Node.js-specific config
- [ ] Document all env vars with defaults
- [ ] Create runbook: common issues, debugging, metrics interpretation
- [ ] Document migration from Python (if any users remain)

---

## 8. Acceptance Criteria for "Production Ready"

The Node.js wrapper is **production-ready** when **ALL** of the following are met:

| # | Criterion | Verification Method |
|---|-----------|---------------------|
| 1 | Zero P0 bugs | All critical bugs fixed, image generation works |
| 2 | Full client probe support | Hermes/Ollama/llama.cpp connect without errors |
| 3 | Feature parity with Python | All endpoints in parity matrix ??? |
| 4 | Configuration parity | All Python env vars supported in Node.js |
| 5 | Metrics durability | No data loss on crash, WAL checkpointing works |
| 6 | Test coverage | Unit + integration tests pass in CI |
| 7 | Load test passed | 100 RPS sustained, <5% p99 latency increase |
| 8 | Chaos test passed | Key exhaustion, model retirement, crash recovery |
| 9 | Monitoring ready | Prometheus + Grafana dashboards operational |
| 10 | Runbook complete | On-call can debug without source diving |

---

## 9. Recommended Migration Sequence

1. **Fix P0 bugs** (Phase 1) ??? Deploy to **staging**
2. **Add probe endpoints** (Phase 2) ??? Verify client compatibility
3. **Run Phase 7 load/chaos tests** on staging
4. **Complete Phases 3-6** in parallel
5. **Blue-green deploy** to production:
   - Run Node.js on port 9101 (beta)
   - Mirror traffic or canary 10%
   - Compare metrics: latency, error rate, token counts
   - Full cutover when metrics match Python baseline
6. **Decommission Python** only after 72h stable on Node.js

---

## 10. Files to Modify (Summary)

| File | Changes Needed |
|------|----------------|
| `src/index.js` | Fix `rawMutated` bug, remove duplicate block, add probe endpoints, add catch-all proxy, make config env-driven |
| `src/key_pool.js` | Add `VERIFY_CONCURRENCY` support, ensure all config from env |
| `src/metrics.js` | Add WAL checkpointing, evaluate native SQLite |
| `src/anthropic_compat.js` | Add `count_tokens` export |
| `package.json` | Add `devDependencies` (jest, etc.), fix test script |
| `.env` | Add all missing env vars with defaults |
| `wrapper-nvidia.service` | Verify port matches code (9100) |
| `test/test.js` | **Create new** - unit test suite |
| `test/integration.test.js` | **Create new** - integration tests |

---

## 11. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `rawMutated` bug causes image gen crashes | **Certain** (code is broken) | **Critical** | Fix immediately (Phase 1) |
| `sql.js` performance degrades at scale | Medium | High | Benchmark; plan migration to `better-sqlite3` |
| Missing probe endpoints break client integrations | **Certain** (Hermes/Ollama probe) | High | Add in Phase 2 |
| No test suite ??? regressions undetected | High | High | Build test suite (Phase 6) |
| Config drift between .env and code | Medium | Medium | Centralize config loading |
| Memory leak in long-running process | Low | High | 24h soak test (Phase 7) |

---

## 12. Sign-Off Checklist

Before declaring Node.js production-ready:

- [ ] All P0 bugs fixed and verified
- [ ] All P1 features implemented and tested
- [ ] Load test results documented (target: <200ms p99 added latency)
- [ ] Chaos test results documented
- [ ] Test suite passes in CI
- [ ] Monitoring dashboards updated for Node.js metrics
- [ ] Runbook reviewed by on-call team
- [ ] Stakeholder sign-off

---

**Prepared by:** GitHub Copilot  
**Date:** 2026-06-27  
**Classification:** Internal - Engineering

---

## 13. Re-Audit Update (2026-06-27 - Post Agent Changes)

Another agent has made changes to the Node.js codebase since the initial audit. This section documents what was fixed and what remains.

### 13.1 Issues FIXED Since Initial Audit

| Item | Status | Details |
|------|--------|---------|
| **`rawMutated` undefined bug** | ??? **FIXED** | Declared at `src/index.js:822` - `const rawMutated = JSON.stringify(body);` |
| **Duplicate/confused code block in image handler** | ??? **FIXED** | Removed erroneous nested block (old lines 715-735) containing vision conversion & param dropping logic |
| **`NVIDIA_GENAI_URL` & `NVIDIA_NVCF_URL` support** | ??? **ADDED** | Imported from `key_pool.js`, used in `index.js` lines 48-49 |
| **`MAX_RETRIES` configurable** | ??? **ADDED** | `src/index.js:51` - `parseInt(process.env.MAX_RETRIES || '5', 10)` |
| **Structured JSON Logging (`emitEvent`)** | ??? **ADDED** | `src/index.js:115-124` - `emitEvent(event, kv)` with `WRAPPER_JSON_LOG=1` support |
| **Observability utilities** | ??? **ADDED** | New files: `src/alert_history.js` (log tailing + alert classification) & `src/loki_push.js` (Loki HTTP push) |

### 13.2 Issues STILL OPEN (Gaps Remaining)

| Priority | Item | Status | Details |
|----------|------|--------|---------|
| **P1** | **Client Probe Endpoints** | ??? **MISSING** | 7 endpoints: `/version`, `/api/version`, `/api/tags`, `/api/v1/models`, `/models`, `/props`, `/v1/props`, `/api/show`, `/favicon.ico` |
| **P1** | **Catch-all Proxy** | ??? **MISSING** | No fallback for `/v1/audio/*`, `/v1/video/*`, `/v1/retrieval/*`, `/v1/genai/*`, `/v1/infer` (async) |
| **P1** | **`/v1/messages/count_tokens`** | ??? **MISSING** | Anthropic token count endpoint |
| **P2** | **WAL Checkpointing** | ??? **MISSING** | `metrics.js` lacks periodic `PRAGMA wal_checkpoint(TRUNCATE)` |
| **P2** | **Config Parity (.env)** | ?????? **PARTIAL** | Still missing: `REQUEST_TIMEOUT`, `DATA_RETAIN_DAYS`, `MODELS_REFRESH_SECONDS`, `MAX_CONNECTIONS`, `ENABLE_PACING`, `UPSTREAM_ROUTES`, `VERIFY_CONCURRENCY`, `WRAPPER_JSON_LOG`, `NVIDIA_GENAI_BASE_URL`, `NVIDIA_NVCF_BASE_URL` |
| **P2** | **Concurrent Model Verify** | ??? **MISSING** | Still sequential (Python: `VERIFY_CONCURRENCY=3`) |
| **P1** | **Test Suite** | ??? **MISSING** | `test/` directory **still empty** |

### 13.3 Updated Feature Parity Matrix (Post-Changes)

| Feature | Python | Node.js (Current) | Status |
|---------|--------|-------------------|--------|
| Core proxy (chat, embeddings, images, ranking) | ??? | ??? | ??? Parity |
| Anthropic `/v1/messages` | ??? | ??? | ??? Parity |
| **Client probe endpoints** | ??? | ??? | **GAP P1** |
| **Catch-all proxy (audio/video/retrieval)** | ??? | ??? | **GAP P1** |
| **`/v1/messages/count_tokens`** | ??? | ??? | **GAP P1** |
| Key pool & rate limiting | ??? | ??? | ??? Parity |
| Model refresh & verification | ??? | ?????? Sequential only | **GAP P2** |
| Metrics (SQLite) | ??? Native | ?????? sql.js (WASM) | **GAP P2** |
| **WAL checkpointing** | ??? | ??? | **GAP P2** |
| **Structured JSON logging** | ??? | ??? `emitEvent` | ??? Parity |
| **Observability utils** | ??? | ??? alert_history, loki_push | ??? Parity |
| Config via env | ??? Full | ?????? Partial | **GAP P2** |
| Test suite | ??? | ??? | **GAP P1** |

### 13.4 Updated Configuration Parity (Post-Changes)

| Env Var | Python | Node.js | Status |
|---------|--------|---------|--------|
| `NVIDIA_API_KEY_*` | ??? | ??? | ??? |
| `SOFT_LIMIT_RPM` | ??? | ??? | ??? |
| `HARD_LIMIT_RPM` | ??? | ??? | ??? |
| `LISTEN_HOST` | ??? | ??? | ??? |
| `LISTEN_PORT` | ??? | ??? | ??? |
| `NVIDIA_BASE_URL` | ??? | ??? | ??? |
| `NVIDIA_GENAI_BASE_URL` | ??? | ?????? Code supports, **missing in .env** | ?????? |
| `NVIDIA_NVCF_BASE_URL` | ??? | ?????? Code supports, **missing in .env** | ?????? |
| `QUEUE_LIMIT` | ??? | ??? | ??? |
| `MAX_RETRIES` | ??? (5) | ??? **Now configurable** | ??? |
| `REQUEST_TIMEOUT` | ??? (600) | ??? Hardcoded 600000ms | ?????? |
| `DATA_RETAIN_DAYS` | ??? (30) | ??? Hardcoded 30 | ?????? |
| `MODELS_REFRESH_SECONDS` | ??? (3600) | ??? Hardcoded 600 | ?????? |
| `KEYS_RELOAD_SECONDS` | ??? (60) | ??? 60000ms | ??? |
| `MAX_CONNECTIONS` | ??? (200) | ??? Hardcoded 80 | ?????? |
| `ENABLE_PACING` | ??? (true) | ??? Hardcoded true | ?????? |
| `PACING_MAX_WAIT` | ??? (60) | ??? 60s | ??? |
| `MODELS_VERIFY_SECONDS` | ??? (86400) | ??? 24h interval | ??? |
| `VERIFY_CONCURRENCY` | ??? (3) | ??? Sequential | ?????? |
| `DROP_PARAMS` | ??? ("think") | ??? ("think") | ??? |
| `UPSTREAM_ROUTES` | ??? JSON | ??? Not implemented | ?????? |
| `WRAPPER_JSON_LOG` | ??? | ?????? Code supports, **missing in .env** | ?????? |
| `KEY_LEVEL_RPM_RATIO` | ??? (0.8) | ??? | ??? |
| `CORROBORATION_WINDOW_S` | ??? (60) | ??? | ??? |
| `MODEL_BLOCK_CAP` | ??? (10) | ??? | ??? |
| `KEY_BLOCK_CAP` | ??? (30) | ??? | ??? |

### 13.5 Updated Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| **Phase 1** | Critical Bug Fixes (P0) | ??? **COMPLETE** - `rawMutated` fixed, duplicate block removed |
| **Phase 2** | Client Probe Endpoints (P1) | ??? **NOT STARTED** |
| **Phase 3** | Missing Features (P1) | ??? **NOT STARTED** - catch-all, count_tokens |
| **Phase 4** | Configuration Parity (P2) | ?????? **PARTIAL** - MAX_RETRIES done, 10+ remaining |
| **Phase 5** | Architecture Hardening (P2) | ??? **NOT STARTED** |
| **Phase 6** | Test Suite (P1-P2) | ??? **NOT STARTED** |
| **Phase 7** | Load & Chaos Testing (P1) | ??? **NOT STARTED** |
| **Phase 8** | Documentation & Runbooks (P2) | ??? **NOT STARTED** |

### 13.6 Updated Acceptance Criteria Progress

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Zero P0 bugs | ??? **MET** |
| 2 | Full client probe support | ??? **NOT MET** |
| 3 | Feature parity with Python | ??? **NOT MET** (3 P1 gaps) |
| 4 | Configuration parity | ??? **NOT MET** (10+ env vars missing) |
| 5 | Metrics durability | ??? **NOT MET** (no WAL checkpoint) |
| 6 | Test coverage | ??? **NOT MET** |
| 7 | Load test passed | ??? **NOT MET** |
| 8 | Chaos test passed | ??? **NOT MET** |
| 9 | Monitoring ready | ?????? **PARTIAL** (JSON logging + Loki utils added) |
| 10 | Runbook complete | ??? **NOT MET** |

### 13.7 Required .env Additions (Current Missing)

```bash
# Multi-host routing (code supports, need in .env)
NVIDIA_GENAI_BASE_URL=https://ai.api.nvidia.com
NVIDIA_NVCF_BASE_URL=https://api.nvcf.nvidia.com

# Retry & timeout
REQUEST_TIMEOUT=600
DATA_RETAIN_DAYS=30

# Model management
MODELS_REFRESH_SECONDS=3600
VERIFY_CONCURRENCY=3

# Connection pool
MAX_CONNECTIONS=200

# Pacing
ENABLE_PACING=true
PACING_MAX_WAIT=60

# Upstream routing (JSON)
UPSTREAM_ROUTES='{}'

# Observability
WRAPPER_JSON_LOG=1
```

### 13.8 Files Modified by Other Agent (Summary)

| File | Changes |
|------|---------|
| `src/index.js` | Fixed `rawMutated` declaration, removed duplicate block, added `emitEvent()`, added `MAX_RETRIES`, `BASE_GENAI`, `BASE_NVCF` |
| `src/key_pool.js` | Exports `NVIDIA_GENAI_URL`, `NVIDIA_NVCF_URL` |
| `src/alert_history.js` | **NEW** - Log tailing, alert classification, deduping |
| `src/loki_push.js` | **NEW** - Loki HTTP push daemon |

### 13.9 Next Recommended Actions (Priority Order)

1. **Immediate (P1 - ~2 hrs):** Implement 7 client probe endpoints in `src/index.js` router
2. **Immediate (P1 - ~1 hr):** Add catch-all proxy handler for multi-modality routes
3. **Immediate (P1 - ~30 min):** Add `/v1/messages/count_tokens` endpoint
4. **Short-term (P2 - ~1 hr):** Add missing 10 env vars to `.env` and wire them in code
5. **Short-term (P2 - ~1 hr):** Add WAL checkpointing to `metrics.js`
6. **Short-term (P2 - ~1 hr):** Implement concurrent model verification (`VERIFY_CONCURRENCY`)
7. **Medium-term (P1-P2 - ~3 hrs):** Create test suite (`test/test.js`, `test/integration.test.js`)
8. **Medium-term (P1 - ~4 hrs):** Load & chaos testing

---

**Re-Audit by:** GitHub Copilot  
**Date:** 2026-06-27  
**Classification:** Internal - Engineering
