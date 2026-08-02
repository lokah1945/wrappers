# CODEX-RESP-02 — OpenAI Responses Streaming Must Parse With the Official SDK

**Date:** 2026-08-01  \
**Branch:** `arena/019fbee0-wrappers`  \
**Scope:** the last Codex bug — `/v1/responses` streaming + non-streaming output
across ALL five wrappers (`nvidia-python`, `nous`, `opencode`, `blackbox`,
`openrouter`) was not parseable by the official openai-python SDK (the exact
code path Codex uses).

---

## 1. Symptom

The previous pass (CODEX-RESP-01) fixed the openrouter reasoning-only hang and
the AI Gateway translation layer. The remaining reported failure: **Codex +
wrapper-nous**. Reproduction against the real wrappers with the openai SDK
stream parser (`client.responses.stream(...)` — what Codex runs) showed **all
five wrappers** emitting streams the SDK rejects or cannot accumulate:

1. `response.created` carried only `{id, model, status}` →
   `AttributeError: 'NoneType' object has no attribute 'append'` on the very
   first `response.output_item.added` (the SDK builds its stream snapshot from
   the created event and appends items to `response.output`).
2. Tool arguments were streamed as **`response.function_call.delta`** — the
   OpenAI Responses API standard is
   **`response.function_call_arguments.delta` / `.done`**. The SDK's
   discriminated union rejects the wrong name, so Codex never accumulated the
   arguments of a tool call.
3. Reasoning items used `summary: ""` (a string) — the SDK's
   `ResponseReasoningItem.summary` is `list[Summary]`; a string triggers
   serializer warnings/failures.
4. Non-streaming `client.responses.create()` responses lacked the SDK-required
   top-level `parallel_tool_calls`, `tool_choice`, `tools` fields →
   `APIResponseValidationError` for any generic OpenAI SDK caller.

The existing E2E harness did not catch this because it validates event
lifecycle with its own parser, not the SDK's.

---

## 2. The Fix (all 5 wrappers)

| # | Fix | Wrapper(s) |
|---|---|---|
| 1 | `response.created` now carries a **full** response object (`object`, `created_at`, `output: []`, `usage` zeros) so the SDK snapshot has a real `output` list | nous, opencode, blackbox (openrouter/nvidia already full) |
| 2 | `response.function_call.delta` → **`response.function_call_arguments.delta`**; added **`response.function_call_arguments.done`** before each tool's `output_item.done` | all 5 |
| 3 | reasoning items use `summary: []` + `content: [{type: "reasoning_text", text}]` (added/done/completed) | all 5 |
| 4 | non-streaming `chat_to_responses`/`respond_non_streaming` include `parallel_tool_calls: true`, `tool_choice: "auto"`, `tools: []` | all 5 (nvidia also via `base_response` for streaming created/completed/failed) |

---

## 3. Proof

`tests/e2e_runtime/sdk_codex_compat.py` boots the mock upstream + all five
wrappers as real uvicorn servers, drives `/v1/responses` with
`tools` / `reasoning_only` / `reasoning` (streaming) and `tools` (non-streaming),
and feeds the raw output into the official openai SDK parser:

```
nvidia-python [tools]: SDK OK (20 typed events)      nous [tools]: SDK OK (18 typed events)
opencode       [tools]: SDK OK (20 typed events)     blackbox [tools]: SDK OK (20 typed events)
openrouter     [tools]: SDK OK (20 typed events)
    tool args streamed via 6 deltas + 2 done events   (every wrapper)
... [reasoning_only] / [reasoning] all SDK OK ...
nvidia-python [nonstream]: SDK OK (['function_call','function_call'])
nous/opencode/blackbox/openrouter [nonstream]: SDK OK (['function_call','function_call','message'])
✅ ALL WRAPPERS × MODES PARSE CLEANLY WITH THE OPENAI SDK (CODEX-RESP-02 FIXED)
```

Unit-level lock-in added to `tests/test_translation_matrix.py`:

- `test_codex_resp02_response_created_carries_full_response_object`
- `test_codex_resp02_no_nonstandard_function_call_event_names` (AST-based)
- `test_codex_resp02_reasoning_summary_is_a_list`
- `test_codex_resp02_nonstreaming_response_has_sdk_required_fields`
- `test_codex_resp02_sdk_parses_wrapper_stream_unit` (real SDK parse, no network)

`openai>=1.40,<3` added to `tests/requirements.txt` for the SDK gate.

---

## 4. Verification Gates (all green)

```bash
python -m pytest tests -q                                    # 209 passed (was 204)
python -m pytest tests/test_translation_matrix.py -q         # 63 passed
python -m pytest tests/test_sse_streaming_regressions.py -q  # 63 passed
python tests/e2e_runtime/run_runtime_e2e.py                  # 445/445 checks
python tests/e2e_runtime/sdk_codex_compat.py                 # 5 wrappers × 4 modes, SDK OK
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6 # ~20.8k reqs, 0 failures
python -m compileall -q common blackbox nous opencode openrouter nvidia-python model-registry tests
```

---

## 5. Conclusion

The "last Codex bug" is fixed at the root: every wrapper's `/v1/responses`
output — streaming and non-streaming, with tools, reasoning, or text — now
parses cleanly with the official openai SDK that Codex uses, and tool
arguments accumulate end-to-end. Combined with the earlier passes (CODEX-RESP-01
reasoning-only completion, F1–F7 translation-layer parity, B-20/B-36/B-39
hardening), the fleet is production-ready as an AI gateway for Claude Code,
Codex, OpenClaw, Hermes Agent, OpenCode, OpenHands, and generic
OpenAI/Anthropic SDK clients.
