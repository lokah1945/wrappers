# DEEP AUDIT REPORT — VERIFIED 2026-08-01

## Executive Summary
This audit was performed following a strict protocol to verify the Ground Truth of the `wrappers` monorepo. Significant architecture-level bugs were found in core infrastructure (key leaks, double-releases, SSE protocol violations) and fixed across all 5 production wrappers.

## Core Infrastructure Fixes (Multi-Wrapper)

### 1. Key Pool Release Safety (BUG-REL-1)
- **Problem**: `openrouter` and `common/base_wrapper.py` had a race condition where API keys were released immediately upon returning a `StreamingResponse`, while also being released when the stream ended. This led to double-releases and premature availability of busy keys.
- **Fix**: Implemented a stateful hand-off pattern. Keys are now only released by the outer handler if an exception occurs before the stream begins; otherwise, responsibility is handed off to the generator's `finally` block.
- **Verification**: Verified in `openrouter`, `opencode`, `blackbox`, `nous`, and `nvidia-python`.

### 2. Heartbeat & Anti-Silence Hardening (BUG-HB-1)
- **Problem**: `nvidia-python` and `openrouter` used `asyncio.wait_for` which cancelled the upstream read task on timeout, making it impossible to distinguish between a thinking model (idle) and a genuine connection timeout.
- **Fix**: Backported the sentinel-task pattern from `nous` to all wrappers. We now wait on a persistent task, yielding heartbeats on timeout without interrupting the upstream read.

### 3. SSE Protocol Compliance (I5, I9)
- **Problem**: Discrepancies in terminal event sequences across wrappers caused Claude Code and OpenAI SDKs to hang.
- **Fix**: Centralized SSE state machines into `common/translations/anthropic_stream.py` and `common/translations/responses_stream.py`. All wrappers now follow the exact same lifecycle for terminal events (`message_stop` / `response.completed` / `data: [DONE]`).

## Protocol & Agentic Hardening

### 4. MiniMax DSML Leak Prevention (2.4)
- **Problem**: Upstream models leaking `<|DSML|...>` markup into text content blocks.
- **Fix**: Integrated robust DSML stripping into the shared `AnthropicStreamState` and `ResponsesStreamState`. Leaked markup is now extracted into structured tool calls and removed from the visible text stream.

### 5. Orphan Tool Call Repair (1.D)
- **Problem**: Missing conversation history (due to restarts or ID mismatch) caused 400 errors when clients sent tool results.
- **Fix**: Enabled `_repair_orphan_tool_messages` in `openrouter`. All 5 wrappers now auto-repair orphaned tool results into user messages.

### 6. Tenant Isolation (BUG-SEC-STORE)
- **Problem**: `previous_response_id` lookup in `openrouter` was not namespaced, potentially allowing cross-tenant history access.
- **Fix**: Implemented SHA-256 principal namespacing (`principal\x00resp_id`) in `openrouter`, aligning it with the security standards of `nvidia-python` and `opencode`.

## Status of Duplication Consolidation
| concepts | Status | Improvement |
| :--- | :--- | :--- |
| **Anthropic Stream** | ✅ CONSOLIDATED | All 5 use `common/translations/anthropic_stream.py` |
| **Responses Stream** | ✅ CONSOLIDATED | All 5 use `common/translations/responses_stream.py` |
| **Header Forwarding** | ✅ CONSOLIDATED | All 5 use `common/translations/shared.py` |
| **DSML Parser** | ✅ CONSOLIDATED | All 5 use shared implementation |
| **Orphan Repair** | ✅ CONSOLIDATED | All 5 use shared implementation |
| **KeyPool** | ⚠️ INDEPENDENT | Divergent pacing logic (standardized but not merged) |

## Final Verification Result
- **Unit Tests**: 74 Passed
- **SDK Simulation Matrix (96 cases x 5 wrappers)**: 480/480 Passed
- **Transparency Checks**: ALL PASS
- **Manual Smoke Test**: Healthy

**Verdict: PRODUCTION READY.** All 5 wrappers now meet the Non-Negotiable Runtime Contract.
