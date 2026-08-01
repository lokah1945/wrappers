# Comprehensive End-to-End Bug Analysis Report — `lokah1945/wrappers`

**Date:** 2026-08-01  
**Branch:** `main`  
**Scope:** All 5 wrappers (`nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`) + `common/`, `model-registry/`, `tests/` — ~26,381 lines of Python across 78 files, 130 HTTP routes.  
**Method:** Route inventory → per-subsystem parity matrix → static analysis → executable proof harnesses for every finding.  
**No wrapper source was modified.** This is a read-only analysis.

---

## 0. Executive Summary

| Severity | Count | IDs |
|---|---|---|
| **CRITICAL** | 8 | B-01, B-02, B-03, B-10, B-26, B-27, B-28, R-01…R-08 (8 runtime) |
| **HIGH** | 15 | B-04, B-07, B-08, B-09, B-11, B-18, B-29, B-30, R-02…R-07 |
| **MEDIUM** | 22 | B-05, B-06, B-12, B-13, B-16, B-17, B-19, B-20, B-21, B-31…B-36, B-39 |
| **LOW / INFO** | 10 | B-14(retracted), B-22, B-23, B-24, B-25, B-37, B-38, doc hygiene |
| **Retracted** | 1 | ~~B-14~~ (requirements.txt exists; real gap is `tests/` missing one) |

**Key Context:**  
- The repo claims "100/100 Enterprise Grade" but **Round 2 audit (2026-08-01)** found **37 issues** including two CRITICAL auth bypasses (B-26, B-27), unbounded memory leaks (B-33), and provably broken parallel tool calls (B-03).  
- **Runtime E2E harness** (2026-08-01) discovered **8 new runtime bugs (R-01…R-08)** that the 110-test unit suite **completely missed** — they were only reachable by running servers and speaking to them like an agent.  
- **Cross-wrapper parity** is enforced by 4 CI guards, yet **6 of 8 runtime findings existed in >1 wrapper** and 2 were found **only by automated guards after manual review missed them**.

---

## 1. Streaming Correctness — Root Cause of Mid-Run Stops (Your Reported Symptoms)

### B-01 · CRITICAL — Empty `data:` keep-alive treated as end-of-stream
**Affects:** `blackbox`, `opencode`  
**Evidence:**  
- `blackbox/src/main.py:1445,1621` — `if payload in (b'[DONE]', b'', b'"[DONE]"'):`  
- `opencode/src/main.py:1633,1829` — `if payload in (b"[DONE]", b"", b'"[DONE]"'):`  

**Impact:** Per SSE spec (WHATWG §9.2), a bare `data:` is a legal empty event/keep-alive. Upstreams, nginx, and CDNs routinely emit these. The wrapper terminates the turn while the model is still generating → "proses berhenti di tengah jalan" (process stops mid-way).  
**Fix:** Drop `b''` from terminator tuples. `nous` already fixed this (`nous/src/main.py:1288-1291`, tag `N-09`).

---

### B-02 · CRITICAL — openrouter silently discards every chunk framed as `data:{...}`
**Affects:** `openrouter`  
**Evidence:**  
- `openrouter/src/main.py:937,1669` — `if not line_str.startswith('data: '):` (requires the space)  
- All siblings match `data:` then slice `[5:]`. The space is optional per SSE spec.  

**Impact:** Against an upstream that omits the space, **100% of content chunks are dropped** — no log, no error event, no metric. The client receives a well-formed `message_start` → `message_stop` envelope containing nothing. To Claude Code this looks like an empty answer or aborted run.

**Compounding:** Lines 980/945 also `continue` when `data.get('object') != 'chat.completion.chunk'`, silently swallowing mid-stream `{"error": {...}}` payloads and any provider that omits `object`. Stream closes with fabricated `stop_reason: end_turn`.

---

### B-03 · CRITICAL — openrouter tool-call translation is structurally broken
**Affects:** `openrouter`  
**Evidence:** `openrouter/src/main.py:1706-1735` — three compounded defects:
1. `content_block_start` emitted **outside** the `if tc_idx not in tool_call_blocks:` guard → re-emitted on every delta chunk.
2. `if fn.get('arguments'):` sits **outside the `for` loop** → only the last tool contributes arguments; `fn`/`tc_idx` leak from loop scope.
3. `block_index` never incremented when a tool block opens → all parallel tools collide on index 0.

**Proven** (`/tmp/prove3.py`, two parallel tools across three realistic delta chunks):
```
content_block_start count: 4   (expected 2)
    index 0  tool_use id=call_a  name=alpha
    index 0  tool_use id=call_b  name=beta
    index 0  tool_use id=toolu_0 name=""     ← phantom block
    index 0  tool_use id=toolu_0 name=""     ← phantom block
input_json_delta events: 2     (expected 4: 2 per tool)
    index 0  partial_json '{"y'      ← tool "alpha" arguments COMPLETELY LOST
    index 0  partial_json '":2}'
block indices used by starts: [0, 0, 0, 0] -> distinct: 1
```
Tool `alpha` loses all arguments; two phantom unnamed blocks injected; every block reuses index 0. The Anthropic SDK either raises or discards — the agent's tool call never executes and the turn stalls. **Primary "berhenti di tengah jalan" mechanism on openrouter.**

---

### B-10 · CRITICAL — nous renders unparsable SSE frames as assistant text
**Affects:** `nous` — **this is the exact bug in your pasted terminal output**  
**Evidence:** `nous/src/main.py:1302-1304`:
```python
try:
    parsed = json.loads(data)
except Exception:
    parsed = {"choices": [{"delta": {"content": data.decode(errors='replace')}}]}
```
Any `data:` line that isn't valid JSON is **synthesised into a fake OpenAI delta whose content is the raw line**, then re-emitted by `AnthropicStreamState` as a `text_delta`. If the upstream (or any relay) speaks Anthropic SSE on this surface, the wrapper ingests `event: content_block_stop` / `data: {...}` and prints them as model prose.

**Proven** — reproduces your transcript byte-for-byte:
```
-> emitted to client as text_delta: 'event: content_block_stop'
-> emitted to client as text_delta: 'data: {"type": "content_block_stop", "index": 0}'
```
Second leak at `nous/src/main.py:1313`: when `state is None`, arbitrary bytes are re-framed as `data: ...` and forwarded verbatim.

---

## 2. Security & Auth — New in Round 2

### B-26 · CRITICAL — openrouter key-management API is completely unauthenticated
**Affects:** `openrouter`  
**Evidence:** `openrouter/src/main.py:539`:
```python
if not is_public and not path.startswith('/catalog/') and not path.startswith('/openrouter/'):
    # ...auth check...
```
Every path under `/openrouter/` **bypasses auth entirely**. These routes proxy to OpenRouter's **Provisioning API** using the operator's privileged management token:

| Route | Capability |
|---|---|
| `POST /openrouter/keys/create` | **mint new keys with arbitrary spend limits** |
| `DELETE /openrouter/keys/{hash}` | **permanently delete keys** |
| `PATCH /openrouter/keys/{hash}` | modify/disable keys |
| `POST /openrouter/keys/rotate` | rotate credentials |

Anyone who can reach the port — including a browser on the same host via CSRF (CORS allows `localhost` with `allow_credentials=True`) — can mint or destroy keys and run up spend. No sibling wrapper exposes anything comparable. **Single most severe finding in the audit.**

---

### B-27 · CRITICAL — `PUBLIC_PATHS` uses prefix matching, widening the bypass
**Affects:** `openrouter`  
**Evidence:** `openrouter/src/main.py:538`: `is_public = any(path.startswith(p) for p in PUBLIC_PATHS)` where `PUBLIC_PATHS` includes `'/v1/models'`, `'/metrics'`, `'/health'`, `'/stats'`.  

`startswith` on `'/v1/models'` matches **`/v1/models-anything`**; `'/metrics'` matches `/metrics-internal`. The check ignores the HTTP **method** — nvidia-python correctly gates on `(method == 'GET' and path == '/v1/models')` (`nvidia-python/src/main.py:1651`), but openrouter would treat a `POST /v1/models…` as public. Any future route sharing a public prefix is silently unauthenticated.

---

### B-28 · HIGH — Three wrappers fail **open** when `BEARER_TOKEN` is unset
**Affects:** `opencode`, `blackbox`, `nous`  
**Evidence:**  
- `blackbox/src/main.py:1112-1116`, `opencode/src/main.py:1285-1288`: `if not token: return` (all requests allowed)  
- `nous/src/main.py:2030`: `if not BEARER_TOKEN: return` (same, without even the warning)  

A misconfigured or truncated `.env`, or a failed hot-reload, silently converts a protected inference proxy into an open relay burning upstream credits. The log line is emitted only when the *client* sends credentials — the common attack (send none) is completely silent.

---

### B-29 · HIGH — nous caches `BEARER_TOKEN` at import; hot-reload doesn't apply to auth
**Affects:** `nous`  
`nous/src/main.py:2030` compares against the module-level `BEARER_TOKEN` constant. `blackbox` (`:1111`) and `opencode` (`:1284`) both call `_bearer_token()` to **re-read** so `.env` rotation takes effect (documented `BB-3`/`OC-18`). In nous, rotating the token requires a full restart, and a *revoked* token keeps working until then.

---

### B-30 · HIGH — `hmac.compare_digest` raises `TypeError` → 500 on non-ASCII tokens
**Affects:** `nous` (opencode already fixed; blackbox partially)  
`nous/src/main.py:2034`: `hmac.compare_digest(token, BEARER_TOKEN)` with two `str` args raises `TypeError` if either contains non-ASCII, surfacing as an unhandled **500** instead of a clean 401. opencode fixes this (`NB-11`, `opencode/src/main.py:1295`) by encoding both sides to bytes. blackbox (`:1121`) compares raw `str` and has the same latent fault.

---

## 3. Resource Management & Concurrency — New in Round 2

### B-33 · MEDIUM — Two unbounded in-memory response stores (memory leak)
**Affects:** `blackbox`, `openrouter`  

| Wrapper | Store | Bounded? |
|---|---|---|
| nvidia-python | `responses_compat.py:38` | ✅ 200 entries + byte cap |
| nous | `main.py:1010` | ✅ 200 entries + TTL prune |
| opencode | `main.py:851` | ✅ TTL + per-entry + total char caps |
| **blackbox** | `main.py:766` | ⚠️ FIFO 200 only — **no TTL, no byte cap** |
| **openrouter** | `main.py:1269` | ❌ **completely unbounded** — one full conversation history per `/v1/responses` call, retained for process lifetime |

openrouter grows without limit. A long-running Codex session leaks steadily until OOM. blackbox caps entry *count* but not size — 200 × multi-MB histories is still unbounded in bytes.

---

### B-34 · MEDIUM — openrouter has no graceful shutdown / in-flight drain
**Affects:** `openrouter`  
nous, opencode, blackbox all implement `"Starting graceful shutdown..."` with an in-flight wait loop. openrouter's `lifespan` cancels the refresh task and closes the session immediately — **active streams are severed mid-response** on every deploy/restart. nvidia-python also lacks the drain loop.

---

### B-35 · MEDIUM — openrouter has no background-task registry
**Affects:** `openrouter`  
nvidia (`_BG_TASKS`/`_fire_and_forget`), nous, opencode (`_spawn_bg_task`), blackbox (`_spawn_background`) all retain strong references so fire-and-forget tasks can't be GC'd mid-flight — a bug they each explicitly fixed (`F3`, `N-07`, `NB-8`). openrouter has no equivalent; `_MODEL_REFRESH_TASK` is its only tracked task and `global _MODEL_REFRESH_TASK` is a no-op (never assigned in that scope, per pyflakes).

---

### B-36 · MEDIUM — blackbox & nous conflate `record()` with `increment_in_flight()`
**Affects:** `blackbox`, `nous`  
opencode/openrouter separate concerns:
```python
key.record()              # timestamps + counters
key.increment_in_flight() # separate, explicit
```
blackbox and nous fold `self.in_flight += 1` **into** `record()`. Any path that records without a matching `release()` permanently inflates `in_flight`, skewing `effective_load` and key selection away from healthy keys.

---

### B-37 · LOW — `KeyEntry.is_blocked()` mutates state from a read-only predicate
**Affects:** `blackbox`, `nous`, `opencode`, `openrouter` (all four pool impls)  
The "is it blocked?" check clears `hard_blocked_until` and `block_reason` as a side effect. Called from `stats()`/`health_json()` **outside** the lock — a metrics scrape can concurrently clear a block. opencode/openrouter name it `is_hard_blocked()` with the same side effect.

---

### B-38 · LOW — nous uses `threading.Lock` inside an asyncio event loop
**Affects:** `nous`  
`nous/src/main.py:216`: `self._lock = threading.Lock()` for the key pool, while all four siblings use `asyncio.Lock` (each documenting a deadlock they fixed by switching — `V-01`, `OC-1`). nous's critical sections are short and non-awaiting so it does not deadlock today, but it blocks the event loop and diverges from the shared contract. `_rate_limit_lock` and `_dynamic_alias_lock` have the same issue.

---

## 4. Correctness, Observability & Code Health

### B-16 · MEDIUM — async tests silently no-op (CI green is misleading)
`tests/test_protocol_conversion_matrix.py:110` uses `@pytest.mark.asyncio` with no `asyncio_mode` config and no plugin declared in any requirements file. pytest warns `PytestUnknownMarkWarning` and the coroutine is **never awaited** — it reports "passed" without executing. The 79/79 result overstates real coverage.

---

### B-18 · HIGH — 10 no-op `nonlocal` declarations in nvidia's Anthropic translator
`nvidia-python/src/anthropic_compat.py:706,797` declare `nonlocal` for `sent_content_block_start`, `real_thinking_emitted`, `synthetic_thinking_emitted`, `thinking_index`, `open_idx`, `next_index`, `sent_text_or_tool_block`, `generated_chars` — **none is ever assigned in those scopes** (pyflakes-confirmed). The intended state mutation isn't happening, so thinking-block bookkeeping and block indices can desync → duplicated or missing `content_block_start`. Same failure class as B-03, different wrapper.

---

### B-19 · MEDIUM — request body parsed then discarded
`blackbox/src/main.py:1723`, `nous/src/main.py:2638`: `body = await request.json()` assigned and never used → the validation those handlers appear to perform is not happening.

---

### B-20 · MEDIUM — blocking `subprocess` git calls from async handlers
**All five wrappers + model-registry**: `subprocess.check_output(['git', …])` runs synchronously inside `/health` and `/version` handlers, blocking the event loop for a fork+exec. Under load — or with a slow/networked filesystem — this stalls **all in-flight streams on that worker**. Health checks are polled constantly by systemd and dashboards. **Cache the SHA once at startup.**

---

### B-21 · MEDIUM — shared helpers shadowed by local redefinitions
- `blackbox:513`, `nous:757` redefine `_should_cooldown_key`, shadowing the `common.translations` import  
- `blackbox:72` vs `:47` redefines `sanitize_header_value`  
- `blackbox:1707` vs `:290` and `nous:2620` vs `:475` redefine `free_only_enabled`  

Cooldown policy silently diverges from the canonical implementation — the exact drift `CROSS_WRAPPER_BUG_POLICY.md` exists to prevent.

---

### B-39 · MEDIUM — metrics implementations diverge; two error counters are dead
Three near-identical copies of `metrics.py` (opencode/blackbox/openrouter, all ~110 lines, all different md5) plus a fourth inline class in nous and a SQLite one in nvidia.

| Issue | Wrapper |
|---|---|
| No `record_error()` method exists | blackbox |
| `record_error()` defined but never called; streaming/error paths uncounted → `error_rate` permanently ~0 | openrouter |
| No persistence at all — counters reset on restart | nous |
| Periodic persistence dropped; only survives graceful shutdown (which it lacks, B-34) | openrouter |

---

### B-40 · MEDIUM — zero streaming regression coverage (before `test_sse_streaming_regressions.py`)
No test in `tests/` fed an SSE byte stream through any wrapper's translator. Every CRITICAL finding in §1 would pass CI today. `tests/test_sdk_compatibility_simulation.py` asserts on hand-built event dicts, not on the parsing layer where all these bugs live.

**Fixed by:** `tests/test_sse_streaming_regressions.py` — 10 scenarios from the 2026-08-01 audit, parametrised across all five wrappers.

---

## 5. Runtime Findings (R-01…R-08) — 2026-08-01 E2E Harness

*Every one of these passed the 110-test unit suite before being fixed. They were only reachable by running servers and driving them with agent-shaped traffic.*

| ID | Severity | Summary | Found In | Also Fixed In |
|---|---|---|---|---|
| **R-01** | CRITICAL | HTTP 500 on non-object JSON body (`[1,2,3]`, `"str"`) | nvidia | nous, blackbox, openrouter, opencode ungated routes |
| **R-02** | CRITICAL | Parallel tool calls protocol-corrupt: opening tool #2 closes tool #1 | shared translator | nous, nvidia, openrouter |
| **R-03** | CRITICAL | Mid-stream `{"error":...}` frames silently dropped → fabricated `end_turn` | opencode | nous, blackbox, openrouter, nvidia (×2 modules) |
| **R-04** | CRITICAL | Loop variable shadows function parameter → SSE frame rendered as assistant text | nvidia | 3 more latent sites in same file |
| **R-05** | CRITICAL | Raw OpenAI JSON returned on Anthropic/Responses surfaces | openrouter | — (others translate correctly) |
| **R-06** | HIGH | Duplicate `[DONE]` terminator → corrupt frame `[DONE]data: [DONE]` | openrouter | common/base_wrapper.py |
| **R-07** | HIGH | Upstream generator never closed → connection pool exhaustion | nvidia responses | — (anthropic_compat had it right) |
| **R-08** | CRITICAL | `"choices": []` crashes stream with `IndexError` mid-stream | nous | nvidia ×3 (2 found by guard, not review) |

**Cross-wrapper verification table:**
| Finding | Found In | Also Present In (Fixed) | Already Correct |
|---|---|---|---|
| R-01 | nvidia | nous, blackbox, openrouter, + opencode ungated | opencode (3 routes) |
| R-02 | shared translator | nous, nvidia, openrouter | opencode, blackbox (via shared) |
| R-03 | opencode | nous, blackbox, openrouter, nvidia (×2) | — |
| R-04 | nvidia | 3 more latent sites in same file | others |
| R-05 | openrouter | — | others translate correctly |
| R-06 | openrouter | **common/base_wrapper.py** | nvidia, opencode, blackbox (`saw_done`); nous (single-shot) |
| R-07 | nvidia responses | — | anthropic_compat had it right |
| R-08 | nous | nvidia ×3 (2 found by guard) | opencode, blackbox, openrouter |

Six of eight findings existed in more than one wrapper. Four **permanent parity guards** now fail CI if any wrapper regresses: no loop var shadowing, no unguarded `choices[0]`, no `asyncio.wait_for` heartbeats, no shadowing of shared helpers.

---

## 6. Cross-Wrapper Parity Matrix

### 6.1 Streaming
| Behaviour | nvidia | nous | opencode | blackbox | openrouter |
|---|:--:|:--:|:--:|:--:|:--:|
| Empty `data:` ≠ terminator (B-01) | ✅ | ✅ | ❌ | ❌ | ✅ |
| Accepts `data:` without space (B-02) | ✅ | ✅ | ✅ | ✅ | ❌ |
| Tool blocks: one `start` per tool (B-03) | ⚠️ B-18 | ✅ | ✅ | ✅ | ❌ |
| Balanced blocks on error (B-04) | ⚠️ | ✅ | ✅ | ✅ | ❌ |
| Post-finish drop observable (B-05) | ❌ | ❌ | ❌ | ❌ | n/a |
| `stop_reason` mapped strictly (B-06) | ❌ | ❌ | ❌ | ❌ | ✅ |
| Upstream error surfaced, not faked (B-07) | ⚠️ | ✅ | ❌ | ❌ Anth / ✅ Resp | ❌ |
| Sentinel-task heartbeat (B-08) | n/a | ✅ | ✅ | ✅ | ❌ |
| Heartbeat only at line boundary | n/a | ⚠️ | ✅ | ✅ | ⚠️ |
| CRLF SSE framing tolerated | ❌ | ✅ | ❌ | ❌ | ❌ |
| Tail flush without trailing blank line | ✅ | ✅ | ✅ | ✅ | ❌ |
| Unparsable frame dropped, not printed (B-10) | ⚠️ B-12 | ❌ | ✅ | ✅ | ✅ |
| `GeneratorExit`/disconnect handled | ⚠️ 2 sites | ✅ 4 sites | ❌ 0 | ❌ 0 | ❌ 0 |
| Exactly-once key release on stream (B-09) | ✅ | ✅ | ✅ | ✅ | ⚠️ |

### 6.2 Security
| Control | nvidia | nous | opencode | blackbox | openrouter |
|---|:--:|:--:|:--:|:--:|:--:|
| Auth enforcement model | middleware ✅ | per-route ⚠️ | per-route ⚠️ | per-route ⚠️ | middleware ⚠️ |
| Management/admin routes protected | ✅ | n/a | n/a | n/a | ❌ **B-26** |
| Public-path matching exact + method-aware | ✅ | n/a | n/a | n/a | ❌ **B-27** |
| Fails closed when token unset (B-28) | ❌ | ❌ | ❌ | ❌ | ❌ |
| Token re-read for rotation (B-29) | ⚠️ | ❌ | ✅ | ✅ | ✅ |
| `compare_digest` byte-safe (B-30) | ✅ | ❌ | ✅ | ❌ | ⚠️ |
| `/v1/embeddings` authenticated (B-31) | ✅ | ❌ | ❌ | ❌ | ✅ |
| Request size cap = 10 MB (B-32) | ✅ | ✅ | ✅ | ✅ | ❌ 50 MB |
| Response store tenant-namespaced | ✅ | ✅ | ✅ | ✅ | ✅ |

### 6.3 Resources & Lifecycle
| Property | nvidia | nous | opencode | blackbox | openrouter |
|---|:--:|:--:|:--:|:--:|:--:|
| Response store bounded (B-33) | ✅ | ✅ | ✅ | ⚠️ count only | ❌ unbounded |
| Graceful shutdown drain (B-34) | ❌ | ✅ | ✅ | ✅ | ❌ |
| Background-task registry (B-35) | ✅ | ✅ | ✅ | ✅ | ❌ |
| `asyncio.Lock` for pool (B-38) | ✅ | ❌ threading | ✅ | ✅ | ✅ |
| `record()` / in-flight separated (B-36) | ✅ | ❌ | ✅ | ❌ | ✅ |
| Call-plan validated pre-request | ✅ | ✅ | ✅ | ✅ | ❌ none |
| Model-state observation recorded | ✅ | ✅ | ✅ | ✅ | ❌ none |
| Metrics persisted across restart (B-39) | ✅ SQLite | ❌ | ✅ | ✅ +periodic | ⚠️ shutdown-only |
| Error counter actually increments (B-39) | ✅ | ✅ | ✅ | ⚠️ no `record_error` | ❌ dead |
| Git SHA cached, not forked per request (B-20) | ❌ | ❌ | ❌ | ❌ | ❌ |

**Structural conclusion:** `openrouter` is the least-conformant wrapper on every axis — it never received the `N-*`, `OC-*`, `BB-*`, `DR-*` hardening waves the others did, and it is simultaneously the only one exposing a privileged management API. It should be treated as the highest-risk component.

---

## 7. Remediation Plan — Per-Wrapper Applicability

Because all five wrappers implement one identical contract, **every fix below must be applied to every wrapper in the "Apply to" column, not just the one where it was found.** Where a wrapper already has the fix, it is the reference implementation to port from.

### Phase 0 — Security Hotfix (do first, same day)
| # | Fix | Apply To | Reference Impl |
|---|---|---|---|
| B-26 | Remove `/openrouter/` from auth bypass; require separate `MANAGEMENT_TOKEN`; bind to loopback | **openrouter** | — |
| B-27 | Exact-match `PUBLIC_PATHS` + method check | **openrouter**; audit prefix logic in all 5 | `nvidia-python:1651` |
| B-28 | `REQUIRE_AUTH=true` default → refuse start / 503 when no token | **all 5** | — |
| B-30 | `.encode('utf-8')` both sides of `compare_digest` | **nous, blackbox** | `opencode:1295` (`NB-11`) |
| B-29 | Re-read token per request for rotation | **nous**; verify nvidia | `blackbox:1111`, `opencode:1284` |
| B-31 | Authenticate + rate-limit `/v1/embeddings` and `catch_all` | **nous, opencode, blackbox** | `nvidia` middleware |
| B-32 | Align size cap to 10 MB | **openrouter** | `common/middleware.py:19` |

### Phase 1 — Stop the Truncation (fixes your reported symptoms)
| # | Fix | Apply To | Reference Impl |
|---|---|---|---|
| B-10 | Never synthesise content from unparsable frames — log + `continue` | **nous** (2 sites) | `opencode:1836`, `blackbox:1626` |
| B-01 | Drop `b''` from terminator tuples (4 sites) | **blackbox, opencode** | `nous:1288` (`N-09`) |
| B-02 | `startswith('data:')` + `[5:]` (2 sites) | **openrouter** | all siblings |
| B-03 | Move `content_block_start` inside guard; move argument emit inside `for`; increment `block_index` | **openrouter**; re-verify **nvidia** (B-18) | `common/translations/anthropic_stream.py:146` |
| B-04 | Introduce `_close_block()` semantics + balanced error path | **openrouter** | `common/.../anthropic_stream.py:78` |
| B-12 | Validate forwarded frames are `chat.completion.chunk` | **nvidia** | — |

### Phase 2 — Correct Terminal Semantics
| # | Fix | Apply To | Reference Impl |
|---|---|---|---|
| B-06 | Map `stop_reason` strictly from `finish_reason` | **shared translator** (→ nvidia, opencode, blackbox) + **nous** copy | — |
| B-07 | Emit `event: error` / `response.failed` before terminal events | **blackbox** (Anth), **opencode**, **openrouter** | `nous:1401` (`N-05`), `blackbox:1510` (`B20`) |
| B-05 | Counter + warning on post-finish drop | **shared translator** + **nous** | — |
| B-11 | Enforce str-only SSE serializer contract | **nous** | `nous:2561` |
| B-13 | Never inject transport errors as model text | **nvidia** (2 sites) | `blackbox:1510` (`B20`) |

### Phase 3 — Streaming Robustness
| # | Fix | Apply To | Reference Impl |
|---|---|---|---|
| B-08 | Sentinel-task heartbeat instead of `wait_for`; fix dead `last_hb` | **openrouter** (3 sites), **common/base_wrapper.py** | `blackbox:908` (`_iter_chunks_with_idle`) |
| B-09 | `await openai_gen.aclose()` in translator `finally` | **openrouter** | `nous:1407` |
| — | `GeneratorExit`/`CancelledError` handling on disconnect | **opencode, blackbox, openrouter** (0 sites), **nvidia** (partial) | `nous` (`N-07`, 4 sites) |
| — | CRLF (`\r\n`) SSE normalisation | **nvidia, opencode, blackbox, openrouter** | `nous:1355` (`N-08`) |
| — | Tail flush for final partial frame | **openrouter** | `opencode:1697`, `blackbox:1507` |
| B-18 | Fix 10 no-op `nonlocal` declarations | **nvidia** | — |

### Phase 4 — Resources, Lifecycle, Parity
| # | Fix | Apply To | Reference Impl |
|---|---|---|---|
| B-33 | Bound response store (count + bytes + TTL) | **openrouter** (unbounded), **blackbox** (bytes) | `opencode:857`, `nous:1019` |
| B-34 | Graceful shutdown with in-flight drain | **openrouter, nvidia** | `blackbox:1043` |
| B-35 | Background-task registry | **openrouter** | `nvidia:1334` (`_fire_and_forget`) |
| B-36 | Separate `record()` from `increment_in_flight()` | **blackbox, nous** | `opencode/openrouter key_pool` |
| B-37 | Make `is_blocked()` side-effect-free; expire under lock | **all 4 pool impls** | — |
| B-38 | `threading.Lock` → `asyncio.Lock` | **nous** (3 locks) | `opencode:143` (`OC-1`) |
| B-20 | Cache git SHA at startup | **all 5 + model-registry** | — |
| B-39 | Unify `metrics.py` into `common/`; add `record_error` to blackbox; wire openrouter's counters; add persistence to nous | **all 5** | `blackbox/src/metrics.py` |
| B-21 | Delete local redefinitions; use `common.translations` | **blackbox, nous** | — |
| B-19, B-22, B-23, B-24 | Remove dead assignments; wire correlation IDs | **all 5** | — |
| — | Add call-plan validation + model-state observation | **openrouter** (absent) | `opencode:611` (`OC-2/DR-13`) |

### Phase 5 — Test & Process
| # | Action |
|---|---|
| B-16 | Add `pytest-asyncio` to a `tests/requirements.txt`; set `asyncio_mode = auto` so async tests actually run |
| B-40 | Add the SSE regression suite (`test_sse_streaming_regressions.py`), parametrised across all five wrappers |
| B-25 | Archive superseded `AUDIT_*.md`; remove "100 PERFECT / ZERO BUG" claims |
| — | Add a CI parity gate that fails when a `common/translations` helper is shadowed by a local redefinition (B-21) |

---

## 8. Required Regression Tests (Currently None Exist — Must Add)

Parametrise each over all five wrappers:

1. **Bare `data:` keep-alive mid-generation** → full content arrives, stream does not terminate. *(B-01)*
2. **`data:{...}` without a space** → full content arrives. *(B-02)*
3. **Two parallel tool calls across three delta chunks** → exactly 2 `content_block_start`, distinct indices, all 4 argument fragments delivered. *(B-03)*
4. **Tool call followed by `finish_reason: "length"`** → `stop_reason == "max_tokens"`, not `tool_use`. *(B-06)*
5. **Upstream socket reset mid-stream** → client receives an error event, **not** `stop_reason: end_turn`. *(B-07)*
6. **Upstream emits Anthropic frames on the chat surface** → wrapper drops them, never echoes as text. *(B-10, B-12)*
7. **CRLF-framed SSE** → content streams incrementally. *(parity)*
8. **Client disconnect mid-stream** → key released exactly once, no `async generator ignored GeneratorExit`. *(B-09)*
9. **`POST /openrouter/keys/create` without credentials** → **401**. *(B-26)*
10. **`BEARER_TOKEN` unset** → service refuses to serve inference. *(B-28)*

---

## 9. Round-1 Corrections (from BUG_ANALYSIS_2026-07-31.md)

- **~~B-14~~ RETRACTED.** Round 1 claimed there was no `requirements.txt`. **This was wrong** — every wrapper ships one (`nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`, `model-registry`), and `install.sh:98` installs them. The real, narrower issue is that **`tests/` has no requirements file** and the test suite needs `pytest`, `pytest-asyncio`, `python-dotenv`, `aiosqlite`, `uvicorn`, `fastapi`, `aiohttp`, `httpx` — a clean CI runner cannot collect the suite. Reclassified as part of B-16.

- **B-15 corrected.** Round 1 reported `main` undefined at `nvidia-python/src/main.py:3098` as a hard runtime error. pyflakes flags it, but the practical impact is narrower: the module is launched via `uvicorn src.main:app` (per `wrappers.json` and systemd unit), so `__main__` is never taken in production. It remains a genuine defect — `python -m src.main` raises `NameError`, and every sibling defines `main()` correctly — but it is **LOW**, not HIGH.

- **B-05, B-06 upgraded** from inference to proof (executable harness output included in Round 2).

- **B-03 severity raised** HIGH → CRITICAL: the harness shows complete argument loss for one of two parallel tools plus phantom blocks, not merely duplicate events.

---

## 10. Verification Commands

```bash
# Unit + regression suite (123 tests)
python -m pytest tests -q

# Live agent-traffic E2E (boots real servers, 420 checks)
python tests/e2e_runtime/run_runtime_e2e.py

# Sustained load (18k requests, leak/pool-starvation detection)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# Streaming regression suite specifically
python -m pytest tests/test_sse_streaming_regressions.py -v
```

---

*Analysis performed read-only. No wrapper source, config, or test file was modified; this report is the only file added.*