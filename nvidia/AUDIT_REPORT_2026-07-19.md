# AUDIT REPORT — wrapper-nvidia (2026-07-19)

- **Repository:** `lokah1945/wrappers`, subtree `nvidia/`
- **Working branch:** `audit-2026-07-19` (fast-forward identical to `main`/`github/main` at `e06127e`)
- **Audit date:** 2026-07-19
- **Auditor:** Principal Backend Engineer (maintainer)
- **Live service:** `wrapper-nvidia` PID 1632666 on `:9100`, executing the **currently deployed** `src/index.js` (pre-fix build). Service intentionally NOT restarted until all validations pass.
- **Scope:** `responses_compat.js` (OpenAI Responses API path used by Codex), its integration with `index.js` (`proxyOpenai`, `translateThinkingToNim`, `normalizeErrorEnvelope`), tool-calling/streaming/error contracts, and documentation consistency.

---

## 1. Method

1. `git status` / `git log --decorate --graph --oneline -30` — confirmed HEAD, active branch, working-tree state.
2. Read the **committed** `responses_compat.js` (HEAD) and the **working-tree** version side by side.
3. Read `index.js` `proxyOpenai` (L1190+), `normalizeErrorEnvelope` (L597), `translateThinkingToNim` (L675), and the `/v1/responses` route (L3467) to establish the real runtime contract.
4. Built a module-level harness loading `createResponsesHandler` with stubbed deps and a fake upstream that emits NIM `reasoning_content` chunks + a final `reasoning` field, covering non-stream, stream, tool-calling, and error passthrough.
5. Ran `npm test` (unit) and `npm run test:e2e` (25/25 mock E2E).
6. Verified documentation claims against implementation (`README.md`, `CHANGELOG.md`, `AUDIT_FIX_REPORT.md`).

---

## 2. Findings

### F1 — Working-tree `responses_compat.js` was based on the pre-fix `.bak` and silently reverted three committed fixes (CRITICAL regression)
**Evidence:** `git diff nvidia/src/responses_compat.js` shows the working tree removes, relative to HEAD, the following already-committed logic:
- `translateThinkingToNim` dependency + the `body.reasoning` → NIM toggle block (HEAD L172-180).
- Bare-string `input` → single user-message handling (HEAD L36-49).
- Faithful upstream error-status mapping in `handleResponsesApi` (HEAD L361-372).

`diff nvidia/src/responses_compat.js.bak.audit-20260719 HEAD:nvidia/src/responses_compat.js` confirms the working tree was edited on top of `responses_compat.js.bak.audit-20260719` (the pre-fix baseline), not on HEAD. The prior agent's "clean patch" therefore **replayed the reasoning feature onto an older base**, discarding the three prior fixes instead of extending them.

**Impact:** If the working-tree file had been deployed, Codex `/v1/responses` requests would regress:
- Bare-string `input` (the common Codex one-shot shape) would no longer become a user message → empty `messages` → NIM 400 "messages field cannot be empty" → wrapper 502. This is the exact Hermes/Codex 502 root cause that was fixed earlier.
- Reasoning models (deepseek-v4-pro, qwen3-thinking, glm, …) reached via `/v1/responses` would never receive the NIM thinking toggle and could hang with no response.
- Upstream client errors (400/422/429) would collapse to a blanket 502 again.

**Fix:** Re-authored `responses_compat.js` from HEAD as the base, then layered the reasoning feature on top. All three prior fixes are preserved and unit-tested.

### F2 — Non-stream error path in HEAD dropped errors (latent bug)
**Evidence:** HEAD's `translateToNim` returned `{ status: result.status, data: result.data }` for non-stream errors, but `handleResponsesApi` only detects errors via `result.error`. `proxyOpenai` returns non-stream errors as `{ status, data: {error:{message,type}} }` (`.error` is nested under `.data`). So in HEAD the non-stream error branch in `translateToNim` produced a shape the caller could not recognize → the error was silently ignored and an empty/null body returned.

**Fix:** `translateToNim` now returns `{ error: { ...result.data.error, status: result.status } }`; `handleResponsesApi` maps it to the faithful HTTP status (upstream status first, else derived from `error.type`). Verified by module test (400→400, 500→500).

### F3 — New streaming reasoning code had an `output_index` collision + missing item in final output (real protocol bug)
**Evidence (working tree):** The reasoning item and the message item were both emitted with `output_index: 0`. The final `response.completed` `output` array was built from `outputs`/`msgId` only and never included the streamed reasoning item, even though it had been opened with `response.output_item.added`. An OpenAI Responses client (Codex) indexes `output` by `output_index`; a duplicate `0` plus a missing item corrupts the event sequence.

**Fix:** Reasoning = `output_index: 0`, message = `output_index: 1`, parallel function calls = `output_index: 2..N`. The reasoning item is opened **lazily** only after the first `reasoning_content`/`reasoning` delta arrives (no dangling open item when there is no reasoning), and is pushed into the final `response.completed` `output` array. Module test asserts: every `output_item.added` has a matching `output_item.done`, reasoning at index 0, message at index 1, and the completed `output` is exactly `['reasoning','message']`.

### F4 — Documentation inconsistencies (not blocking, but required deliverables)
- `README.md` declares `Version: 8.6.0-node` and `Branch: fix/audit-2026-07-06`, but the active branch is `audit-2026-07-19` and `package.json` version is `8.6.0`. `CHANGELOG.md` top entry is `[8.6.1] - 2026-07-06`.
- `README.md` endpoint tables omit `/v1/responses` (actually routed at `index.js` L3467).
- `README_AGENT.md` did not exist; required by deliverables.

**Fix:** Corrected `README.md` (version, branch, added `/v1/responses` row). Created `README_AGENT.md` reflecting actual implementation. Added `CHANGELOG.md` entry `8.6.2` (2026-07-19).

### F5 — Minor capability-accuracy observation (no code change required)
`/v1/capabilities` reports `supports_function_calling: true` for `meta/llama-3.2-11b-vision-instruct` via heuristic; that vision model rejects tool payloads upstream (400). This is a catalog-accuracy nuance, not a wrapper protocol bug; no hardcode fix introduced (would contradict the capability-driven design). Noted for future capability-source refinement.

---

## 3. Root Cause

The regression in F1 was caused by the prior agent re-basing its edit on a stale backup file (`responses_compat.js.bak.audit-20260719`) instead of the committed HEAD, while treating "apply reasoning feature" as a rewrite rather than an additive diff. The practice of editing via a separate `.mjs`/`.bak` round-trip, without re-diffing against current HEAD, let the three prior fixes fall out of the working tree. F2/F3 are ordinary implementation defects in the same file (error-shape mismatch against `proxyOpenai` contract; SSE index bookkeeping).

---

## 4. Validation Results

### 4.1 Module-level tests (new code, against `createResponsesHandler`)
Harness: stubbed `pool`, `resolveTargetModel`, `proxyOpenai`, `forwardHeaders`, `translateThinkingToNim`, `describe`, `CURATED_GENAI`; fake `res` capturing `.write()`.

| # | Case | Result |
|---|---|---|
| 1 | Non-stream with `reasoning_content` | reasoning item at `output[0]`, message at `output[1]` ✅ |
| 2 | Non-stream plain (no reasoning) | only message item, no reasoning ✅ |
| 3 | Non-stream tools | `function_call` item(s), no reasoning ✅ |
| 4 | Upstream 400 → 400; 500 → 500 | faithful status passthrough ✅ |
| 5 | Stream with `reasoning_content` | reasoning idx 0, message idx 1, in final `output`, every `added` has `done` ✅ |
| 6 | `reasoning` control present | `translateThinkingToNim` invoked ✅ |

### 4.2 Suite tests
- `npm test` (unit): **All pass**.
- `npm run test:e2e` (mock NIM): **25/25 pass**.

### 4.3 Live endpoint smoke (deployed OLD build, informational only)
Live `/v1/capabilities` and `/v1/models` respond 200 (static endpoints). Because the service still runs the pre-fix build, live `/v1/responses` checks reflect OLD behavior and are **not** used to validate the new code. New-code validation is via the module harness (§4.1) and the full suite (§4.2).

---

## 5. Deliverables Produced
- Corrected `src/responses_compat.js` (preserves prior fixes + adds reasoning correctly).
- `AUDIT_REPORT_2026-07-19.md` (this file).
- `README_AGENT.md` (actual implementation + client configs).
- `CHANGELOG.md` updated with `8.6.2`.
- `README.md` corrected (version, branch, `/v1/responses` endpoint).
- Atomic commits on `audit-2026-07-19` (logic separate from docs).
- **Service NOT restarted** until all validations passed.

---

## 6. Production-Ready Status

**Conditional PASS.** Implementation, tests, reasoning feature, and audit are now mutually consistent. Remaining pre-merge action: apply the atomic commits on `audit-2026-07-19` and run a single restart + live smoke of `/v1/responses` (non-stream + stream, reasoning on/off, tools) to confirm the deployed behavior matches §4.1. No protocol regression remains in the code.
