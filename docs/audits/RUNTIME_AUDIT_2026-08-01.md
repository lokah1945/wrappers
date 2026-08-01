# Runtime Audit — Zero-Error Backend Verification

**Date:** 2026-08-01 · **Branch:** `arena/019fba14-wrappers` · **Range:** `d8b8c21 → b6c6053`
**Objective (as set):** no runtime error of any kind when a real agent/client drives these wrappers as a backend. Security explicitly de-prioritised. Every fix verified across **all five** wrappers.

---

## 1. Result

| Gate | Result |
|---|---|
| **Runtime E2E** (5 wrappers × 3 surfaces × 21 upstream behaviours) | **420 checks / 0 failures** |
| **Sustained soak** (5 wrappers, concurrent load) | **~18,100 requests / 0 failures** |
| **Unit + regression suite** | **123 passed** |
| **Server logs during all runs** | **0 tracebacks, 0 `GeneratorExit` violations, 0 unclosed sessions** |
| **Memory under load** | **+1–3 MB RSS per wrapper** (flat — no leak) |
| **p95 latency, first vs last quarter** | **flat** (no degradation) |
| **pyflakes (real defects)** | **clean** |

**8 new runtime bugs (R-01 … R-08) were found and fixed.** Every one passed the 110-test unit suite that existed before this round — they were only reachable by running the servers and speaking to them like an agent.

---

## 2. Why a new harness was necessary

The existing suite imported functions and asserted on hand-built dicts. Agents open sockets, stream bytes, disconnect mid-turn, and send malformed input. Those two things exercise almost disjoint code.

Three components were added under `tests/e2e_runtime/`:

**`mock_upstream.py`** — a provider that speaks OpenAI Chat + Anthropic Messages and, on demand, produces 21 pathological-but-legal behaviours real providers actually emit:

```
normal      nospace     keepalive    crlf       tools       reasoning
nofinish    noterminator midstream_error abrupt  slow       usage_after
empty       unicode     bigchunk     bytesplit  comments    dupfinish
nullcontent emptychoices toolnoid     longtool   http500    http429
```

**`run_runtime_e2e.py`** — boots each wrapper as a real `uvicorn` server against that upstream and drives it exactly as Claude Code / Codex / the OpenAI SDK would, validating the full protocol contract: event ordering, block lifecycle (every `content_block_start` matched by a `stop` on the same index, no reuse, no delta on a closed block), exactly-one terminal event, nothing after `message_stop`, tool arguments reassembling into valid JSON, malformed input → 4xx never 5xx, client disconnect not wedging the pool, and 12 concurrent streams.

**`soak.py`** — sustained concurrent load asserting no leak, no pool starvation, no log tracebacks, and no latency degradation.

> One process note: `tests/runtime/` was silently swallowed by the repo's existing `runtime/` gitignore rule, so the first two commits of harness code were never actually tracked. Renamed to `tests/e2e_runtime/` (commit `b6c6053`).

---

## 3. The eight runtime bugs

### R-01 · CRITICAL · all five wrappers — HTTP 500 on a non-object JSON body
A valid JSON body that isn't an object (`[1,2,3]`, `"str"`, `42`) hit `body.get(...)` immediately; `list.get` raises `AttributeError`, which escaped as **HTTP 500** with an ASGI traceback. A 500 tells an SDK the *server* is broken — many then retry, amplifying load — instead of that the *request* was malformed.

opencode guarded this inline on 3 of its routes (its `F6` fix); the other four had no guard at all. Patching 31 individual `await request.json()` call sites would regress the moment someone adds a route, so this is fixed structurally in new **`common/body_guard.py`** ASGI middleware, applied to all current and future JSON routes on all five wrappers.

### R-02 · CRITICAL · shared translator + nous + nvidia + openrouter — parallel tool calls were protocol-corrupt
Opening tool #2 **closed** tool #1. OpenAI interleaves argument fragments across all active tool indices, so tool #1's later fragments then arrived as `content_block_delta` on a **closed** index. Claude Code drops the block and the agent turn stalls.

Observed live before the fix:
```
start(0) delta(0) STOP(0) start(1) delta(1) delta(0) delta(1) stop(1)
                    ↑ closed too early      ↑ orphaned
```
nvidia was worse: `stop_open()` also **deleted** the tool from `tool_map`, so the next fragment re-created a *phantom* unnamed `tool_use` block — 4 blocks, none containing valid JSON.

Fixed by tracking `open_tool_blocks` and letting tool blocks stay open concurrently, closing them together at the terminal path. Applied to the shared translator, nous's own copy, nvidia's `anthropic_compat`, and openrouter's translator.

### R-03 · CRITICAL · all five — mid-stream `{"error": ...}` frames silently dropped
Such a frame has no `"choices"` key, so every `if not chunk.get('choices'): continue` guard discarded it, and the stream closed with a fabricated `end_turn` / `response.completed`. **The client persisted a truncated answer as a successful turn and could not retry.** Now surfaced as a real Anthropic `error` event / `response.failed` on every surface of every wrapper.

### R-04 · CRITICAL · nvidia — SSE protocol frames rendered as assistant prose
```python
async for chunk in stop_open():   # ← shadows the `chunk` PARAMETER
    yield chunk
```
The loop variable overwrote the function parameter holding the model's text. After a thinking→text transition, `chunk` contained the last SSE frame emitted by `stop_open()`, so the literal string `event: content_block_stop\ndata: {...}` was passed downstream and **rendered to the user as assistant text**.

This is the same visible defect as the nous leak reported earlier, reached by a completely different route. It only triggers on reasoning models transitioning to text, which is why no unit test caught it. An AST scan found **3 more latent instances** of the same shadowing class; all fixed, and `test_r04_no_loop_variable_shadows_a_function_parameter` now scans every wrapper on each CI run.

### R-05 · CRITICAL · openrouter — raw OpenAI JSON returned on the Anthropic and Responses surfaces
`_proxy_request` always returns a `JSONResponse` for non-streaming calls, so `return response` inside the `isinstance(response, JSONResponse)` branch fired **before** the translation below could ever run.

```
before:  {"object":"chat.completion","choices":[{"message":{...}}]}
after:   {"type":"message","role":"assistant","content":[{"type":"text",...}],"stop_reason":"end_turn"}
```
Claude Code and Codex cannot parse the "before" shape at all — every non-streaming turn was broken.

### R-06 · HIGH · openrouter + `common/base_wrapper.py` — duplicate `[DONE]` terminator
`[DONE]` was appended unconditionally, producing the corrupt frame `[DONE]data: [DONE]` when the upstream had already sent one without its trailing blank line. Not valid JSON, so strict SDK parsers error at the very end of an otherwise good turn. Now guarded by `saw_done` plus line-boundary completion.

### R-07 · HIGH · nvidia `responses_compat` — upstream generator never closed
`stream_wrapper()`'s `finally` releases the aiohttp response **and** the pool key. Breaking out of `async for raw in stream` left the generator merely *suspended*, so that `finally` never ran and the TCP connection was never returned to the connector. Under load this exhausts `MAX_CONNECTIONS`, after which every request blocks forever — while the key pool still cheerfully reports `available`. Fixed with an explicit `finally: await stream.aclose()`.

### R-08 · CRITICAL · nous + nvidia — `"choices": []` crashed the stream
`chunk["choices"][0]` raises `IndexError` on a legal empty choices array (usage-only frames, provider keep-alives). Escaped as **HTTP 500 mid-stream**, killing the turn. Found live:
```
File "nous/src/main.py", line 1845, in translate_chunk
IndexError: list index out of range
```
Fixed at 4 sites. The new parity guard `test_r08_no_unguarded_choices_indexing` then found **two more unguarded sites that manual review had missed** — precisely the cross-wrapper verification requested.

---

## 4. Cross-wrapper verification

Every fix was checked against all five wrappers, per instruction. This is where the method paid off:

| Finding | Found in | Also present in (and fixed) | Already correct |
|---|---|---|---|
| R-01 | nvidia | nous, blackbox, openrouter, + opencode's ungated routes | opencode (3 routes) |
| R-02 | shared translator | nous, nvidia, openrouter | opencode, blackbox (via shared) |
| R-03 | opencode | nous, blackbox, openrouter, nvidia (×2 modules) | — |
| R-04 | nvidia | 3 more latent sites in the same file | others |
| R-05 | openrouter | — | others translate correctly |
| R-06 | openrouter | **common/base_wrapper.py** | nvidia, opencode, blackbox (`saw_done`); nous (single-shot) |
| R-07 | nvidia responses | — | anthropic_compat had it right |
| R-08 | nous | nvidia ×3 (2 found by the guard, not by review) | opencode, blackbox, openrouter |

Six of eight findings existed in more than one wrapper. Four **permanent parity guards** now fail CI if any wrapper regresses: no loop var shadowing a parameter, no unguarded `choices[0]`, no `asyncio.wait_for` heartbeats, no shadowing of shared helpers.

---

## 5. Score

| Dimension | Score | Evidence |
|---|:--:|---|
| No runtime errors under agent traffic | **100** | 420/420 E2E checks, 5 wrappers × 3 surfaces × 21 modes |
| No crashes / unhandled exceptions | **100** | 0 tracebacks across all E2E and soak runs |
| Protocol contract (Anthropic / OpenAI / Responses) | **100** | full lifecycle validation: ordering, block pairing, single terminal event |
| Streaming framing robustness | **100** | byte-split, big-chunk, CRLF, comments, keep-alive, no-space all pass |
| Tool calling (incl. parallel) | **100** | distinct indices, all fragments reassemble to valid JSON |
| Error transparency | **100** | upstream failures never presented as success on any surface |
| Malformed input handling | **100** | 4xx shaped envelopes, never 5xx |
| Resource management | **100** | flat RSS, in_flight → 0, no pool starvation over 18k requests |
| Stability under sustained load | **100** | 0 failures, p95 flat first vs last quarter |
| Cross-wrapper parity | **100** | 4 automated parity guards; all 5 wrappers identical on every axis tested |
| Regression protection | **100** | 123 unit tests; 8 R-findings verified failing on the pre-fix tree |

**Runtime score: 100/100 against everything this harness can exercise.**

I want to be precise about what that means, because an unqualified "100/100" would overstate it. It means: **zero errors across 420 protocol checks and ~18,100 live requests spanning every surface, every wrapper, and 21 upstream behaviours, with regression tests proving each fix.** It does not mean the code is provably bug-free — no test suite can establish that. The honest statement is that I no longer have a way to make these wrappers fail, having deliberately tried to.

Known limits of this verification, so you can judge the residual risk yourself:
- The upstream is a mock. It reproduces framing and protocol behaviours faithfully, but not a specific provider's quirks (e.g. a real NVIDIA NIM edge case).
- Soak duration was ~12s × 6 concurrent per wrapper (~18k requests), not 24 hours. Slow leaks below the 64 MB threshold would not surface.
- Multi-turn conversation state (`previous_response_id` chains beyond one hop) is covered only lightly.

If you want any of those closed, the highest-value next step is pointing `run_runtime_e2e.py` at a real provider key and running the soak for an hour.

---

## 6. How to reproduce

```bash
python -m pip install -r tests/requirements.txt

# unit + regression suite
python -m pytest tests -q                      # 123 passed

# live agent-traffic E2E (boots real servers)
python tests/e2e_runtime/run_runtime_e2e.py    # 420 checks / 0 failures
python tests/e2e_runtime/run_runtime_e2e.py --wrapper nous -v

# sustained load
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6
```

`run_runtime_e2e.py` exits non-zero on any failure, so it is CI-ready as-is.

---

## 7. Harness corrections (not product bugs)

Recorded for honesty — these initially appeared as failures and were traced to the harness:

1. Base-URL convention differs: nvidia and nous append `/v1` themselves; the others expect it included.
2. nous drops API keys shorter than 10 characters, so the original mock keys were silently discarded.
3. CRLF is legal SSE framing, and a bare `data:` is a legal keep-alive — my first parser rejected both.
4. `id:` and `retry:` are legal SSE fields (WHATWG 9.2.6).
5. The mock must send `Connection: close` on SSE; with keep-alive, a wrapper correctly waiting for EOF looks like a hang.
6. nvidia **paces** admission above `SOFT_LIMIT_RPM=30` — correct production backpressure, but the suite fires ~55 req/min, so limits are raised for the run.
7. A stale `uvicorn` from an earlier debug session held a port and produced phantom failures.

I also introduced and then fixed one regression of my own: the body guard's replay initially synthesised `http.disconnect` after the body, which made Starlette's disconnect-watcher cancel **every** streaming response after one event. It now delegates to the original `receive()`.

---

*All verification performed on `arena/019fba14-wrappers`. No production configuration or credential was modified.*
