# Deep Audit Report — 2026-08-04 Round 13 (from-zero re-audit of the dark areas; clean round)

**Scope**: ninth re-audit "from zero" (R12 committed, contract v3.3, all 9
gates green). Per the standing instruction, the audit restarted at zero
with no findings assumed closed. This round (1) re-read the code areas with
the least line-by-line coverage so far ("dark areas") hunting entire bug
*classes* not tied to any previous finding, and (2) re-ran **all 9 contract
gates on the committed tree** to prove the R12 state end-to-end.

**Result**: **0 new defects. Clean round.** Every inspected site already
carries the correct guard (most from R1–R12 fixes re-verified in place).
All 9 gates re-verified green on the exact committed tree.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 312 passed |
| `run_runtime_e2e.py` | ✅ 990 checks |
| `sdk_codex_compat.py` | ✅ clean (5 wrappers × 4 modes) |
| `compat_layer_e2e.py` | ✅ L2+L3 all 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable, 0 server-log issues |
| `agent_loop_e2e.py` | ✅ 55 checks |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks, zero cross-talk |
| `fuzz_bodies_e2e.py` | ✅ 1081 checks |

---

## Targeted sweeps (code re-read, this round)

### S-13.1 — nous upstream retry chain (`post_nous` / `post_nous_with_retries`)
Full read of the OAuth-token-then-static-key-pool ladder. Verified:
stream-response ownership transfers **exactly once** (the `released = True`
hand-off to the stream generator; the `finally` releases on every other
path), retriable-vs-terminal discrimination, 429 `Retry-After` cooldown
parsing into key cooldowns, model-scoped (N-12) vs key-scoped failure
marking, jittered inter-attempt backoff, `exclude=tried_labels` so a retry
never re-selects a just-failed key, and the OAuth retry-after preserved in
the terminal error message (BUG-M1). Network exceptions shape into 502 —
never leak a stack. **No defect.**

### S-13.2 — Pool double-release / negative in-flight (all 5 wrappers)
Every `KeyEntry.decrement_in_flight` floors at zero
(`if self.in_flight > 0`), every pool `release` routes through the pool
`asyncio.Lock` (exactly-once accounting, CONTRACT §6.1), and every wrapper
runs a periodic `heal_in_flight` loop (30–300 s) that would recover even a
hypothetical missed release. Streaming generators in
openrouter/blackbox/opencode all pair `except (GeneratorExit,
asyncio.CancelledError)` with a `finally` that releases the aiohttp
response **and** the pool key. **No defect.**

### S-13.3 — Client header forwarding (`build_forward_headers`, shared)
Allowlist-based (RFC hop-by-hop stripped; wrapper-owned headers —
`authorization`, `content-length`, `content-type`, `host`,
`accept-encoding` — always set by the wrapper itself). Client `x-api-key`
is **not** forwarded upstream (not allowlisted) → no credential leakage to
the provider; `anthropic-version`/`anthropic-beta` pass through for the
Claude-Code surface. **No defect.**

### S-13.4 — nvidia `/v1/responses` store (`responses_compat`)
Store keys are principal-namespaced (`_store_key`), TTL-evicted, byte- and
entry-bounded, and read/written as **deep copies in both directions**
(R8 parity). Response ids are minted by `_rand('resp')`
(10-char base-36 random + ms-hex suffix) — unique per mint, no reuse of
upstream `chatcmpl-*` ids as store keys (the R7/P0 class). `random.choices`
is not a CSPRNG but draws from a 36^10 space, far below the birthday bound
at any realistic request volume. **No defect.**

### S-13.5 — model-registry service (full critical-path re-read)
R9 guards verified in place: `providers()` guarded snapshot for `/health`,
`bind_aliases` under the `threading.RLock` via `asyncio.to_thread`,
`list_models` snapshots under the guard (MR-2 class closed), request-size
limiter mounted, provider-name validation on every endpoint (MR-4).
`state()`'s unguarded dict *read* is safe under the GIL (entries are
append-only). **No defect.**

### S-13.6 — Generic class sweeps (all wrappers + common/)
- `while True` loops: all are background loops with `await
  asyncio.sleep(...)` minimum-interval floors, or stream chunk iterators
  bounded by socket-read timeouts that terminate upstream EOF. **No
  unbounded hot loop.**
- Silent `except Exception: pass/return None` in request paths: **zero
  occurrences** in the five wrappers (remaining catches either re-raise
  `CancelledError` first, log, or shape into an error payload).
- Metrics double-count on mid-stream faults: `stream_passthrough`-style
  finalizers count `_premature_emitted`/generator exceptions exactly once
  (B-39 class), verified in the current code.

## Negative result statement

No bug — not even a low-severity one — survived this round's sweeps. The
remaining risk is concentrated where it cannot be eliminated by reading:
live-provider behaviour (real NVIDIA/Nous/OpenCode/BLACKBOX/OpenRouter
upstreams, not the mock). All in-repo behaviour is pinned by 9 gates and
re-verified here.

**Conclusion**: the repository remains **9/9 gates green, 0 failures** on
the committed v3.3 tree; round 13 closes with a clean bill. Multi-agent,
multi-client concurrent operation (Claude Code, Codex, OpenClaw, Hermes,
opencode, OpenHands + any OpenAI/Anthropic SDK client) is verified
end-to-end with zero cross-talk and zero leaked reservations.
