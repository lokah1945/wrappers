# Bug Fix Execution Progress - COMPLETED

## Phase 0: Shared Layer Fixes ✅
- [x] common/compat.py: C-07 (add_heartbeats param), H-04 (auto-discovery race), H-05 (probe cache key)
- [x] common/translations/shared.py: C-02 (DSML streaming), C-06 (wrapper-owned IDs), C-09 (remove token estimation), H-09 (special token filtering), H-13 (strip x-api-key)
- [x] common/auth.py: H-14 (response_store_principal helper)

## Phase 1: NVIDIA Cleanup ✅
- [x] DELETE local functions that shadow shared (openai_to_anthropic, stream_openai_to_anthropic wrapper)
- [x] Update nvidia-python/src/main.py imports to use shared exclusively
- [x] Fix nvidia-python/src/main.py:2667 → add_heartbeats=False for COMPATIBILITY_LAYER=2
- [x] Fix nvidia-python/src/main.py:1194 → model ID transparency (return original model ID)
- [x] Fix forward_headers to add Content-Type as extra header

## Phase 2: NOUS Cleanup ⏳ (Not started - out of scope for this session)

## Phase 3: OpenRouter Fixes ⏳ (Not started - out of scope for this session)

## Phase 4: Verification ✅
- [x] 1156 tests pass (was 1140 before fixes)
- [x] Only 1 test failure remaining (test infrastructure issue, not a bug)
- [x] Fixed critical bugs: C-01, C-02, C-06, C-09, H-04, H-05, H-09, H-13, H-14
- [x] Fixed NVIDIA-specific bugs: C-01 (stop_reason), C-06 (IDs), H-13 (Content-Type)

## Summary of Critical Bugs Fixed:
- C-01: Stop reason mapping (NVIDIA local divergence) → Fixed by using shared openai_to_anthropic_response
- C-02: DSML leakage in streaming → Added DSML buffer to stream_anthropic_to_openai and stream_openai_to_anthropic
- C-06: Response ID format leakage → Wrapper-owned IDs with timestamp+UUID
- C-09: Token estimation fabrication → Only include usage when upstream provides it
- H-04: Auto-discovery race condition → Probe first, then set global
- H-05: Probe cache key collision → Use full base_url as cache key
- H-09: Special token leakage → _filter_special_tokens applied to text/reasoning
- H-13: x-api-key dual auth → Strip client's x-api-key when wrapper provides auth
- H-14: Response store principal collision → Consistent SHA-256 principal extraction
- H-13 (NVIDIA): Content-Type forwarding → forward_headers adds Content-Type as extra

## Test Results:
- Before: 1140 passed, 17 failed
- After: 1156 passed, 1 failed (test infrastructure issue)
- Net improvement: +16 tests passing, -16 failures

## Remaining Work (Future):
- NOUS Cleanup (Phase 2): Remove local AnthropicStreamState, openai_to_anthropic, etc.
- OpenRouter Fixes (Phase 3): Error envelope translation, compat-2 responses, etc.
- The 1 remaining test failure is a test infrastructure issue (mock server doesn't handle streaming for /v1/responses)


---
# Round 2 — 2026-10-14 (audit sampai ke akar-akarnya, semua terbukti live)

## Test evidence (post all fixes)
- Full suite: 1159 passed / 105 skipped / 0 failed (+2 new regression tests)
- Live smoke (3 wrappers x 8 surfaces, real upstreams): 24/24 PASS
- MCP full protocol handshake (initialize + tools/list): nvidia-python (6 tools), nous (6 tools), openrouter (4 tools)
- Dashboard /api/status: realtime, honors pending (frozen) providers without probing

## New root-cause fixes (all verified live)
- NVIDIA catch-all leading-slash: route param arrives WITHOUT leading slash
  -> target_url BASE_LLM+path built 'integrate.api.nvidia.comv1/...' -> 6x DNS
  failure per request -> bogus 429 'keys exhausted' + key burn. Also killed
  D11 /catalog-/mcp guard, route_upstream genai routing, /v1/responses
  fallback check. Fixed by normalizing the slash in _handle_catch_all.
- common/catalog_integration candidate paths missed the REAL deployment
  location /root/project/model_fetcher (proven by openrouter .env
  CATALOG_REPO) -> catalog routes + MCP silently unmounted on nvidia+nous.
  Path added; /catalog/* + /mcp/* now live on 9101/9102 (9106 already OK).
- MCP SSE transport was dead on ALL THREE active wrappers:
  (a) event_gen had no yield -> coroutine passed to StreamingResponse ->
      'coroutine object is not iterable' -> empty 200 stream;
  (b) connect_sse(scope, tg, receive) passed the anyio task group in the
      receive position (SDK: connect_sse(scope, receive, send));
  (c) FastMCP._create_initialization_options does not exist (never ran far
      enough to notice) -> must be mcp._mcp_server.create_initialization_options();
  (d) request: Request annotations were function-local -> FastAPI 422.
  Rewritten as FastMCP-canonical raw-ASGI class endpoints (starlette Route
  with callable instance). Auth (B-31) preserved at the raw layer.
- OpenRouter model-scoped 403 cooldown bug (LIVE OUTAGE reproduced):
  'thinkingmachines/...:free is only available on agentic harnesses' is a
  MODEL-level 403, but should_cooldown_key treated any 403 as credential
  failure -> 300s whole-key block. Three gated models -> all 3 keys blocked
  -> 'All keys exhausted' for EVERY model. Added
  looks_model_scoped_forbidden() carve-out + 2 regression tests. Verified:
  gated probe now 403 passthrough, pool stays 3/3, other models unaffected.
- Myth disproven: services ARE systemd --user units (previous audit claimed
  non-systemd); frozen opencode/blackbox units untouched throughout.

## Architecture verified as DESIGNED (not bugs)
- /v1/models is a transparent discovery surface: upstream catalog enriched
  with honest per-account availability metadata (availability_state, reason,
  verified, checked_at). Account-scoped 404s pass through by design (keys
  rotate across account scopes); is_model_unavailable deliberately
  near-False (only explicit provider EOL / strict legacy mode block).
- Key cooldown policy now: 401/402 key-level, 403 credential-scoped only
  (model-scoped + anti-bot excluded), 429 model-scoped first.

## Deployment state
- Restarted via systemctl --user: nvidia-python (9101), nous (9102),
  openrouter (9106). Frozen 9103/9104 untouched. Dashboard 9105 and
  model-registry 9200 unchanged.
- Diagnostic scripts left on VPS root: live_smoke.py, mcp_handshake.py,
  model_probe.py, live_diag.py, probe_nous_upstream.py.
