# PATCH PLAN - WRAPPER-NVIDIA v8.6.0-node
## Structured Fix Plan untuk Zero-Downtime Production Readiness

---

## 🎯 PRINSIP UTAMA

1. **JANGAN UBAH KONSEP DASAR** - Transparent proxy, parameter pass-through, multi-key rotation sudah benar
2. **FOKUS RELIABILITY** - Zero-downtime, zero-error runtime
3. **PERTAJAM YANG ADA** - Strengthen existing capabilities
4. **SUPPORT SEMUA MODEL NIM** - Chat, Vision, Embed, Image, Rerank, Audio, Video, OCR, Parse

---

## 📋 PRIORITAS 0 - CRITICAL (1 Hari - Blocker Production)

### P0-1: inFlight Counter Leak Fix
**Files**: `key_pool.js:84-92`, `src/index.js:137-143, 735-742, 950-963`
**Issue**: Exception paths skip `decInFlight()` → counter leaks → false load shedding → 503
**Fix**: Wrap ALL acquire/release dalam try/finally blocks

```javascript
// Pattern to apply everywhere:
try {
  incInFlight();
  key.incrementInFlight();
  // ... do work ...
} finally {
  decInFlight();
  key.decrementInFlight();
}
```

**Affected locations**: 
- `proxyOpenai()` lines 739, 950, 962
- `proxyPost()` lines 1046, 1217, 1235
- `handleCatchAll()` lines 1691, 1903, 1971
- `handleChatCompletions()` streaming finally block
- `handleAnthropicMessages()` streaming finally block

**Test**: Run 1000 concurrent requests, verify `inFlight` returns to 0

---

### P0-2: DEFAULT_TEMPERATURE 0.2 → 0.7
**File**: `.env:62`
```env
# Before
DEFAULT_TEMPERATURE=0.2
# After
DEFAULT_TEMPERATURE=0.7
```
**Reason**: 0.2 terlalu deterministik untuk coding. Claude Code default 1.0, 0.7 balanced.

**Test**: Verify Anthropic→OpenAI translation preserves client temperature, only injects default when missing

---

### P0-3: Tool Call ID Generation - Anti Collision
**File**: `src/anthropic_compat.js:471`
```javascript
// Before
const toolCallId = tc.id || `toolu_${Math.random().toString(36).slice(2, 10)}${ai}`;

// After  
const toolCallId = tc.id || `toolu_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}_${ai}`;
```
**Reason**: Timestamp + random + index = globally unique across turns/sessions

**Test**: Generate 10000 IDs, verify no duplicates

---

### P0-4: SSE Heartbeat Timer Leak Fix
**File**: `src/anthropic_compat.js:359-506`

**Fix**: Call `clearHeartbeat()` di SEMUA exit paths:
```javascript
// Add at top of finally block:
clearHeartbeat();

// Also add after normal stream completion (before yield message_stop):
clearHeartbeat();
```

**Test**: Run 100 streaming requests, verify no timer leaks (check process handles)

---

### P0-5: Model Verification Flapping Fix
**File**: `src/index.js:421-580`

**Changes**:
```javascript
// Increase grace period
const MODEL_GRACE_FAILS = 5;  // Was 2

// Add exponential backoff for verification
const VERIFY_BACKOFF_BASE = 30000;  // 30s base
// In verifyModels(): wait longer between probes for problematic models
```

**Reason**: Transient NIM issues cause flip-flop. 5 failures with backoff = stable.

**Test**: Monitor model status over 1 hour, verify no flip-flop

---

### P0-6: EADDRINUSE Handling
**File**: `src/index.js:2913-2914`

```javascript
// Add before server.listen()
serverInstance.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`[FATAL] Port ${LISTEN_PORT} already in use. Is another instance running?`);
    process.exit(1);
  }
});
```

**Test**: Start two instances, second should exit cleanly with message

---

## 📋 PRIORITAS 1 - HIGH (Minggu 1 - Production Ready)

### P1-1: Vision Model Classification - Complete Coverage
**File**: `src/capabilities.js:111`

**Add missing patterns**:
```javascript
{ patterns: [
    'vila', 'neva', '-vision', 'vision-', 'paligemma', 'kosmos', 'llava',
    'florence', 'phi-3-vision', 'phi-3.5-vision', 'phi-4-multimodal',
    'nvclip', 'fuyu', 'deplot', 'pix2struct', 'git-base', 'git-large',
    'mm-reasoner', 'qwen2-vl', 'qwen-vl', 'internvl', 'cogvlm',
    'internlm-xcomposer', 'gemma-3', 'llama-3.2-vision', 'pixtral',
    'molmo', 'aria', 'nemotron-3-vision', 'nemotron-vision'
  ], type: 'vision_chat' },
```

**Also add embedding/rerank/audio patterns** for new NIM models.

**Test**: Verify `/v1/models` returns correct `supports_vision: true` for all vision models

---

### P1-2: Context Window - Dynamic from Metadata
**Files**: `src/index.js:39, 1538`, `src/capabilities.js`

**Approach**: Fetch actual context window from model metadata
```javascript
// In enrichModelMetadata():
context_window: hasContextWindow 
  ? (desc.context_window ?? desc.metadata?.context_window ?? DEFAULT_CONTEXT_WINDOW) 
  : undefined,
```

**Fallback**: If metadata unavailable, probe with binary search or use conservative default per model family.

**Test**: Compare returned context_window with actual NIM limits

---

### P1-3: Streaming Buffer 128KB → 8KB → 512KB
**Files**: `src/index.js:1263, 1828`

```javascript
// Before
const MAX_STREAM_BUFFER = 128 * 1024;
// After
const MAX_STREAM_BUFFER = parseInt(process.env.MAX_STREAM_BUFFER_MB || '512', 10) * 1024 * 1024; // 512KB default
```

**Also add**: Backpressure handling jika client slow consume

**Test**: Stream 1MB+ response, verify no buffer overflow

---

### P1-4: Model Block Starvation Fix
**File**: `key_pool.js:486-500`

**Fix**: Per-model block tidak boleh trigger load shedding untuk model lain
```javascript
// In acquireSlot():
const modelSaturated = model && avail.every(s => {
  const kml = this._keyModelLimit[`${s.label}/${model}`];
  return kml !== undefined && this.keyModelRpm(s.label, model) >= Math.max(1, Math.floor(kml * 0.9));
});

// Only shed if ALL models on ALL keys saturated
if (modelSaturated) {
  wait = 1.0;
} else if (avail.length === 0) {
  // No keys at all - shed
} else {
  // Has available keys for other models - proceed normally
}
```

**Test**: Block model A on all keys, verify model B still works

---

### P1-5: Timeout Chain Alignment
**Files**: `.env:67-70`, `src/index.js:2906-2911`

```env
# Aligned timeouts (all in ms for consistency)
REQUEST_TIMEOUT_MS=180000          # 3 min - max total request
TTFT_TIMEOUT_MS=120000             # 2 min - first token
ANTI_SILENCE_TIMEOUT_MS=240000     # 4 min - server socket (REQUEST + 60s buffer)
SERVER_KEEPALIVE_TIMEOUT_MS=65000  # 65s - keepalive
SERVER_HEADERS_TIMEOUT_MS=35000    # 35s - headers
```

**Rule**: `ANTI_SILENCE >= REQUEST_TIMEOUT + 60s`, `SERVER_* >= REQUEST_TIMEOUT + buffer`

**Test**: Slow upstream (5min), verify clean timeout at 3min not 4min

---

### P1-6: message_stop Logic Simplification
**File**: `src/index.js:1413-1443`

**Replace complex logic dengan state machine**:
```javascript
// Stream state tracking
const streamState = {
  started: false,
  hasContent: false,
  stopEmitted: false,
  errorEmitted: false
};

// In stream handler:
if (!streamState.started) { emitStart(); streamState.started = true; }
if (content) { streamState.hasContent = true; }
if (finish_reason && !streamState.stopEmitted) { 
  emitStop(); streamState.stopEmitted = true; 
}
if (error && !streamState.errorEmitted) {
  emitError(); streamState.errorEmitted = true; 
  // Per Anthropic spec: error is terminal, NO message_stop after
}
```

**Test**: Normal stream, error mid-stream, client disconnect, empty stream

---

### P1-7: Dual Delta Race Condition Fix
**File**: `src/anthropic_compat.js:417-452`

**Fix**: Atomic block index management
```javascript
// When both reasoning AND content in same delta:
if (reasoning && contentText) {
  // Emit thinking FIRST (if not already), then text
  if (thinkingIndex === null) { yield* emitThinkingStart(); }
  yield* emitThinkingDelta(reasoning);
  if (textIndex === null) { yield* emitTextStart(); }
  yield* emitTextDelta(contentText);
} else if (reasoning) { ... } else if (contentText) { ... }
```

**Test**: NIM response with both reasoning_content + content in single delta

---

### P1-8: CORS Methods Restriction
**File**: `src/index.js:2018`

```javascript
// Before
res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
// After
res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
```

---

### P1-9: SSRF Protection for Vision Images
**File**: `src/index.js:248-277`

```javascript
// Add before fetch:
const BLOCKED_IPS = ['127.0.0.1', '169.254.169.254', '10.', '172.16.', '192.168.'];
const url = new URL(imgUrl);
if (BLOCKED_IPS.some(ip => url.hostname.startsWith(ip))) {
  console.warn(`[SSRF] Blocked internal IP: ${url.hostname}`);
  continue; // skip this image
}
```

---

## 📋 PRIORITAS 2 - MEDIUM (Bulan 1 - Scale & Hardening)

### P2-1: sql.js → better-sqlite3 Migration
**File**: `src/metrics.js`

**Steps**:
1. Add `better-sqlite3` to package.json
2. Rewrite Metrics class menggunakan native SQLite
3. Enable WAL mode: `db.pragma('journal_mode = WAL')`
4. Add connection pooling untuk concurrent writes
5. Benchmark: target 10x write throughput

**Impact**: 2.2GB DB → fast startup, concurrent writes, no WASM overhead

---

### P2-2: Dynamic Routing from Capabilities
**Files**: `src/index.js:357-399`, `src/capabilities.js`

```javascript
// Replace hardcoded UPSTREAM_ROUTES_SORTED dengan:
function getUpstreamRoute(path, model) {
  const desc = describe(model, BASE_LLM, BASE_GENAI);
  const ep = desc.endpoints?.find(e => path.startsWith(e.path));
  return ep?.base_url || resolveBase(model);
}
```

**Benefit**: New NIM endpoints work without code deploy

---

### P2-3: Circuit Breaker Upstream
**Files**: `src/index.js`, `key_pool.js`

```javascript
// Per upstream host circuit breaker
const circuitBreaker = {
  failures: 0,
  lastFailure: 0,
  state: 'closed', // closed | open | half-open
  threshold: 5,
  timeout: 30000
};

// In proxyOpenai/proxyPost:
if (circuitBreaker.state === 'open') {
  if (Date.now() - circuitBreaker.lastFailure > circuitBreaker.timeout) {
    circuitBreaker.state = 'half-open';
  } else {
    throw new Error('Circuit breaker open');
  }
}
```

**Benefit**: Prevent cascade failures when upstream degraded

---

### P2-4: Request Tracing (Trace IDs)
**File**: `src/index.js`

```javascript
// Propagate trace ID to upstream
const traceId = req.headers['x-request-id'] || generateRequestId();
headers['X-Request-ID'] = traceId;
headers['X-B3-TraceId'] = traceId; // Zipkin compatible

// Add to metrics:
metrics.recordRequest({ ..., traceId });
```

**Benefit**: End-to-end debugging, correlation with NIM logs

---

### P2-5: Large Body Streaming JSON Parse
**File**: `src/index.js:181-218`

```javascript
// For bodies > 10MB, use streaming parse
async function* parseStreamingJSON(stream) {
  // Use clarinet or similar streaming JSON parser
  // Yield partial objects as parsed
}
```

**Benefit**: Avoid 100MB memory spike for large tool_result payloads

---

### P2-6: Health/Readiness Probes
**Files**: `src/index.js`

```javascript
// Add new endpoints:
if (path === '/health/live') return handleLiveness(res);  // Process alive
if (path === '/health/ready') return handleReadiness(res); // Upstream reachable, keys available

async function handleReadiness(res) {
  const keysOk = pool.availableKeys > 0;
  const upstreamOk = await checkUpstreamHealth();
  if (keysOk && upstreamOk) return jsonResp(res, 200, { status: 'ready' });
  return jsonResp(res, 503, { status: 'not ready', keys: keysOk, upstream: upstreamOk });
}
```

**Benefit**: Kubernetes readiness/liveness probes

---

### P2-7: Prometheus Metrics Cardinality Control
**File**: `src/metrics.js`, `key_pool.js`

```javascript
// Limit label cardinality
const MAX_MODEL_LABELS = 100; // Only track top N models
const MAX_KEY_LABELS = 20;

// In promMetrics(): aggregate low-volume models/keys into "other"
```

**Benefit**: Prevent Prometheus OOM dari label explosion

---

## 📋 PRIORITAS 3 - POLISH (Quarter - Operational Excellence)

### P3-1: Clustering Support (PM2/cluster)
**Architecture**: Multi-process dengan shared key pool via Redis atau file-based coordination

### P3-2: Admin API untuk Key Management
**Endpoints**: `POST /admin/keys`, `DELETE /admin/keys/:id`, `POST /admin/keys/reload`

### P3-3: Model Capability Negotiation
**Feature**: Auto-detect model features via probe, cache results

### P3-4: Graceful Drain SIGTERM
**File**: `src/index.js:2929-2952`

```javascript
// Wait for in-flight with progress logging
const drainInterval = setInterval(() => {
  if (inFlight === 0) { clearInterval(drainInterval); process.exit(0); }
  console.log(`[drain] Waiting for ${inFlight} requests...`);
}, 1000);
```

### P3-5: Structured JSON Logging
**Replace** `console.log/warn/error` dengan structured logger:
```javascript
const log = { info, warn, error } = (level, msg, meta) => {
  console.log(JSON.stringify({ level, msg, ts: Date.now(), ...meta }));
};
```

---

## ✅ DEFINITION OF DONE PER PRIORITAS

### P0 Done When:
- [ ] `npm test` passes
- [ ] 1000 concurrent requests → inFlight returns to 0
- [ ] Temperature default 0.7 verified
- [ ] Tool call IDs unique across 10k generations
- [ ] No SSE timer leaks (100 streams)
- [ ] Model status stable 1 hour (no flip-flop)
- [ ] EADDRINUSE exits cleanly

### P1 Done When:
- [ ] All vision models detected correctly
- [ ] Context window accurate per model
- [ ] 1MB+ streaming works
- [ ] Model A blocked ≠ Model B blocked
- [ ] Timeout chain aligned (test slow upstream)
- [ ] message_stop state machine passes all edge cases
- [ ] Dual delta handled correctly
- [ ] CORS restricted
- [ ] SSRF blocked

### P2 Done When:
- [ ] better-sqlite3 migration complete, 10x write perf
- [ ] Dynamic routing works for new endpoints
- [ ] Circuit breaker prevents cascade
- [ ] Trace IDs propagated end-to-end
- [ ] 50MB body parses without OOM
- [ ] Health/ready endpoints work
- [ ] Prometheus labels < 1000

### P3 Done When:
- [ ] PM2 cluster mode works
- [ ] Admin API manages keys hot
- [ ] Model features auto-detected
- [ ] SIGTERM drains gracefully
- [ ] Structured logs in production

---

## 🚀 EXECUTION ORDER

```
Week 1:  P0-1 → P0-2 → P0-3 → P0-4 → P0-5 → P0-6
         P1-1 → P1-2 → P1-3 → P1-4 → P1-5 → P1-6 → P1-7 → P1-8 → P1-9

Week 2-4: P2-1 → P2-2 → P2-3 → P2-4 → P2-5 → P2-6 → P2-7

Quarter:  P3-1 → P3-2 → P3-3 → P3-4 → P3-5
```

---

## 🧪 TESTING CHECKLIST PER RELEASE

### Pre-commit (Local):
```bash
npm test                    # Unit tests
npm run lint               # If exists
node test/integration.js   # Integration tests (new)
```

### Pre-push (CI):
```bash
# Unit + Integration + Load
npm test
npm run test:integration
npm run test:load          # 500 concurrent, 5 min
npm run test:chaos         # Network partition, 5xx, 429
```

### Post-deploy (Production):
```bash
curl /health/live
curl /health/ready
curl /metrics/prom | grep -E 'in_flight|upstream_latency'
# Monitor dashboard 10 min
```

---

*Patch Plan v1.0 | Target: Zero-Downtime Production | Concept: Transparent Proxy NVIDIA NIM*