# Prioritized Remediation Plan

**Date:** 2026-08-01  
**Basis:** Deep audit findings (00-09) with cross-wrapper parity requirement  
**Principle:** Every fix must be applied to ALL affected wrappers, not just where found

---

## Phase 0 — Security Hotfixes (DO FIRST — Same Day)

| # | Fix | Apply To | Reference Implementation | Effort | Risk if Deferred |
|---|---|---|---|---|---|
| **B-26** | Remove `/openrouter/` from auth bypass; require dedicated `MANAGEMENT_TOKEN`; bind mgmt routes to loopback only | **openrouter** | nvidia middleware pattern | 2h | **CRITICAL** — Unauthenticated key provisioning (mint/delete keys, financial loss) |
| **B-27** | Exact-match `PUBLIC_PATHS` + method gating (replace `startswith`) | **openrouter**; audit all 5 | nvidia-python:1651 | 1h | **CRITICAL** — Prefix bypass exposes internal routes |
| **B-28** | `REQUIRE_AUTH=true` default → 503 when no token configured (fail closed) | **ALL 5** | — | 1h | **HIGH** — Truncated .env = open relay burning credits |
| **B-30** | `.encode('utf-8')` both sides of `hmac.compare_digest` | **nous, blackbox** | opencode:1295 (NB-11) | 30m | **HIGH** — Non-ASCII token → 500 instead of 401 |
| **B-29** | Re-read token per request (`_bearer_token()`) for rotation/revocation | **nous**; verify nvidia | blackbox:1111, opencode:1284 | 30m | **HIGH** — Revoked token works until restart |
| **B-31** | Authenticate + rate-limit `/v1/embeddings` + `catch_all` | **nous, opencode, blackbox** | nvidia middleware | 1h | **MEDIUM** — Unauthenticated CPU/memory work |
| **B-32** | Align request size limit to 10 MB (fleet standard) | **openrouter** | common/middleware:19 | 15m | **MEDIUM** — 50 MB + weak auth = max attack surface |

**Total Phase 0 Effort: ~6 hours**

---

## Phase 1 — Stop the Truncation (Fixes Reported Symptoms)

| # | Fix | Apply To | Reference Implementation | Effort |
|---|---|---|---|---|
| **B-10** | Never synthesize content from unparsable frames — log + `continue` (2 sites in nous) | **nous** | opencode:1836, blackbox:1626 | 1h |
| **B-01** | Drop `b''` from terminator tuples (4 sites: blackbox×2, opencode×2) | **blackbox, opencode** | nous:1288 (N-09) | 30m |
| **B-02** | `startswith('data:')` + `[5:]` (2 sites in openrouter) | **openrouter** | All siblings | 30m |
| **B-03** | Move `content_block_start` inside guard; move argument emit inside `for`; increment `block_index` | **openrouter**; re-verify **nvidia** (B-18) | common/translations/anthropic_stream.py:146 | 2h |
| **B-04** | Introduce `_close_block()` semantics + balanced error path | **openrouter** | common/translations/anthropic_stream.py:78 | 1h |
| **B-12** | Validate forwarded frames are `chat.completion.chunk` before yielding | **nvidia** | — | 1h |

**Total Phase 1 Effort: ~6 hours**

---

## Phase 2 — Correct Terminal Semantics

| # | Fix | Apply To | Reference Implementation | Effort |
|---|---|---|---|---|
| **B-06** | Map `stop_reason` strictly from `finish_reason` (remove `tool_map` inference) | **shared translator** (→ nvidia, opencode, blackbox) + **nous** copy | openrouter implementation | 2h |
| **B-07** | Emit `event: error` / `response.failed` before terminal events on exception | **blackbox** (Anth), **opencode**, **openrouter** | nous:1401 (N-05), blackbox:1510 (B-20) | 2h |
| **B-05** | Counter + warning on post-finish content drop (observability) | **shared translator** + **nous** | — | 1h |
| **B-11** | Enforce str-only SSE serializer contract (nous Responses) | **nous** | nous:2561 (Anthropic path) | 30m |
| **B-13** | Never inject transport errors as model text (2 sites in nvidia) | **nvidia** | blackbox:1510 (B-20) | 1h |

**Total Phase 2 Effort: ~6.5 hours**

---

## Phase 3 ��� Streaming Robustness

| # | Fix | Apply To | Reference Implementation | Effort |
|---|---|---|---|---|
| **B-08** | Sentinel-task heartbeat instead of `wait_for`; fix dead `last_hb` assignment | **openrouter** (3 sites), **common/base_wrapper.py** | blackbox:908 (`_iter_chunks_with_idle`) | 2h |
| **B-09** | `await openai_gen.aclose()` in translator `finally` blocks | **openrouter** (3 translators) | nvidia responses_compat.py | 1h |
| **GeneratorExit** | Add `except (GeneratorExit, CancelledError): raise` + cleanup in `finally` | **opencode, blackbox, openrouter** (0 sites each); **nvidia** (partial) | nous (N-07, 4 sites) | 2h |
| **CRLF** | Normalize `\r\n` → `\n` in chat streaming parsers | **nvidia, opencode, blackbox, openrouter** | nous:1355 (N-08) | 1h |
| **Tail flush** | Flush final partial frame without trailing blank line | **openrouter** | opencode:1697, blackbox:1507 | 30m |
| **B-18** | Fix 10 no-op `nonlocal` declarations in nvidia Anthropic translator | **nvidia** | — | 1h |

**Total Phase 3 Effort: ~7.5 hours**

---

## Phase 4 — Resources, Lifecycle, Parity

| # | Fix | Apply To | Reference Implementation | Effort |
|---|---|---|---|---|
| **B-33** | Bound response store on 3 axes (count + bytes + TTL) | **openrouter** (was unbounded), **blackbox** (missing byte cap) | opencode:857, nous:1019 | 2h |
| **B-34** | Graceful shutdown with in-flight drain (30s default) | **openrouter, nvidia** | blackbox:1043 | 1h |
| **B-35** | Background-task registry for ALL fire-and-forget | **openrouter** | nvidia:1334 (`_fire_and_forget`) | 1h |
| **B-36** | Separate `record()` from `increment_in_flight()` | **blackbox, nous** | opencode/openrouter key_pool | 1h |
| **B-37** | Make `is_blocked()` side-effect-free; explicit `expire_block()` under lock | **ALL 4 pool impls** | — | 2h |
| **B-38** | `threading.Lock` → `asyncio.Lock` (3 locks in nous) | **nous** | opencode:143 (OC-1) | 1h |
| **B-20** | Cache git SHA at startup (module level) | **ALL 5 + model-registry** | — | 1h |
| **B-39** | Unify `metrics.py` into `common/`; add `record_error` to blackbox; wire openrouter counters; add persistence to nous | **ALL 5** | blackbox/src/metrics.py | 3h |
| **B-21** | Delete local redefinitions; use `common.translations` | **blackbox, nous** | — | 1h |
| **B-19,22,23,24** | Remove dead assignments; wire correlation IDs | **ALL 5** | — | 1h |
| **Call-plan** | Add call-plan validation + model-state observation | **openrouter** (absent) | opencode:611 (OC-2/DR-13) | 2h |

**Total Phase 4 Effort: ~16 hours**

---

## Phase 5 — Test & Process

| # | Action | Effort |
|---|---|---|
| **B-16** | Add `pytest-asyncio` to `tests/requirements.txt`; set `asyncio_mode = auto` | 30m |
| **B-40** | Ensure SSE regression suite (`test_sse_streaming_regressions.py`) runs in CI parametrised across all 5 | 1h |
| **B-25** | Archive superseded `AUDIT_*.md`; remove "100 PERFECT / ZERO BUG" claims from docs | 1h |
| **Parity Guard** | Add CI gate: fail when `common/translations` helper shadowed by local redefinition (B-21) | 1h |

**Total Phase 5 Effort: ~3.5 hours**

---

## Total Remediation Effort: ~39.5 hours

---

## Required Regression Tests (Must Add to CI)

Parametrise each over all 5 wrappers:

| # | Test | Finding | Reference |
|---|---|---|---|
| 1 | Bare `data:` keep-alive mid-generation → full content arrives, no termination | B-01 | `test_b01_*` |
| 2 | `data:{...}` without space → full content arrives | B-02 | `test_b02_*` |
| 3 | Two parallel tools across 3 chunks → 2 starts, distinct indices, 4 arg fragments | B-03 | `test_b03_*` |
| 4 | Tool call + `finish_reason: "length"` → `stop_reason == "max_tokens"` | B-06 | `test_b06_*` |
| 5 | Upstream socket reset mid-stream → error event, not `end_turn` | B-07 | `test_b07_*` |
| 6 | Anthropic frames on chat surface → dropped, never echoed as text | B-10, B-12 | `test_b10_*` |
| 7 | CRLF-framed SSE �� incremental streaming | Parity | `test_b08_crlf_framing_normalised` |
| 8 | Client disconnect mid-stream → key released once, no `GeneratorExit` leak | B-09 | (new) |
| 9 | `POST /openrouter/keys/create` without creds → 401 | B-26 | `test_b26_*` |
| 10 | `BEARER_TOKEN` unset → 503 on inference endpoints | B-28 | `test_b28_*` |

---

## Reference Implementation Mapping

| Component | Reference Wrapper | Files to Port |
|---|---|---|
| Auth Middleware | nvidia-python | `src/main.py:1648` auth_middleware |
| SSE Parsing | common/sse.py | `iter_chunks_with_idle`, `normalize_sse_newlines` |
| Anthropic Translation | common/translations/anthropic_stream.py | `AnthropicStreamState` class |
| Shared Cooldown | common/translations/shared.py | `should_cooldown_key` |
| Key Pool | opencode/src/key_pool.py | `KeyEntry`, `KeyPool` with separate accounting |
| Metrics | blackbox/src/metrics.py | `Metrics` class with SQLite/JSON persistence |
| Response Store | openrouter/src/main.py:1609 | Bounded `OrderedDict` with TTL + bytes |
| Graceful Shutdown | blackbox/src/main.py:1043 | Drain loop + task await |
| BG Task Registry | nvidia-python/src/main.py:1334 | `_fire_and_forget` + `_BG_TASKS` |

---

## Verification Checklist (Per Wrapper)

After each phase, verify per wrapper:

```bash
# 1. Unit + parity + regression
pytest tests -q                    # 127 tests

# 2. Streaming regressions (parametrised)
pytest tests/test_sse_streaming_regressions.py -v

# 3. Live E2E
python tests/e2e_runtime/run_runtime_e2e.py

# 4. Soak
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# 5. Contract conformance
pytest tests/test_sse_streaming_regressions.py::test_contract_all_wrappers_expose_required_surfaces -v
```

---

## Ownership & Tracking

| Phase | Owner | Target Date | Status |
|---|---|---|---|
| Phase 0 | Security team | Day 0 | 🔴 Not Started |
| Phase 1 | Streaming team | Day 1 | 🔴 Not Started |
| Phase 2 | Protocol team | Day 2 | 🔴 Not Started |
| Phase 3 | Streaming team | Day 3 | 🔴 Not Started |
| Phase 4 | Platform team | Day 5 | 🔴 Not Started |
| Phase 5 | QA team | Day 6 | 🔴 Not Started |

**Definition of Done:** All 5 wrappers pass 420 E2E checks, 10k+ soak requests, 0 tracebacks, parity guards green.

---

*Remediation plan derived from audit findings 00-09. Every fix cross-referenced to reference implementation. Parity requirement: fix propagates to ALL affected wrappers.*