# Deep Audit Report — 2026-08-04 Round 17 (COMPATIBILITY_LAYER=3 never honored — the biggest finding)

**Scope**: twelfth re-audit "from zero", triggered by a *flaky gate*: the
compat-layer E2E intermittently failed `openrouter layer=3 vs Anthropic`
with upstream 404. Chasing the flake to ground truth surfaced a dormant
P1 product defect plus two harness defects.

**Result**: **1 product defect (P1) + 2 harness defects fixed.**
**325 unit · 990 runtime · 55 agent-loop · 10 concurrency · compat gate now
genuinely verifies auto-discovery on all 5 wrappers × both dialects, 3/3
back-to-back stable.**

---

## B-17.1 (P1) — COMPATIBILITY_LAYER=3 auto-discovery was a no-op in all 5 wrappers

Contract §9.2 promises: layer `3` = "Auto Discovery (probe upstream once
per base URL, cache with `COMPATIBILITY_PROBE_TTL_SEC`, fall back to 1)".

Ground truth before this round — three compounding failures:

1. **Probe results were never consumed.** All 15 request-routing branch
   points (3 surfaces × 5 wrappers) tested `_is_anthropic_upstream()`,
   which reads ONLY the env var (`compat_layer() == '2'`). Under layer 3
   that is always False → every wrapper silently behaved as layer 1:
   OpenAI-dialect forwarding to an Anthropic-native upstream → upstream
   404 on every call (exactly the gate's flake symptom).
2. **The probe itself was broken for v1-style bases.**
   `probe_upstream_compatibility` posts `{base}/v1/messages`; with
   `OPENROUTER_BASE_URL=http://host/v1` (OpenRouter/OpenCode/BLACKBOX
   style) that becomes `/v1/v1/messages` → 404 at every probe step → even
   a consumed probe would have defaulted to OpenAI.
3. **No wrapper exercised layer 3 except openrouter, and only in one
   test** — which was itself passing for the wrong reason (below).

**Fix (shared, §7):** `common/compat.py` gains
`resolve_upstream_is_anthropic(session_or_getter, base_url)` — layers 1/2
resolve without network; layer 3 awaits the TTL-cached probe, inconclusive
→ OpenAI (contract fallback). `_probe()` now normalizes the base
(`_probe_base` strips one trailing `/v1`) so root- and v1-style bases probe
identically. Each wrapper defines a 3-line glue
(`_upstream_is_anthropic()` / nvidia `_resolve_is_anthropic_for(session)`
passing its pool session + base constant) and all 15 call sites now await
it. Degraded-mode (ImportError fallback) wrappers keep env-only behavior.

**Proof**: `tests/e2e_runtime/compat_layer_e2e.py` now boots **all 5
wrappers × both mocks** under layer 3 and asserts dialect-correct output
(chat-completions shape vs OpenAI mock; `type=message` vs Anthropic mock).
10/10 scenario checks pass; 3 consecutive back-to-back runs stable. Unit
coverage: 9 new resolver/probe tests in
`tests/test_compat_layer_resolver.py` (canned sessions — v1-base
Anthropic detection, root-base detection, OpenAI detection, unreachable
fallback, explicit layers never touch network, async session getter).

## B-17.2 (harness) — compat gate layer-3 race masked the defect for its whole life

The old layer-3 block `p.terminate(); time.sleep(0.5)` then
`p2.terminate()` — never reaping the process nor waiting for the port to
actually close. All five wrappers drain in-flight requests gracefully on
SIGTERM, so `/health` probes after terminate hit the **dying previous
process**: `vs Anthropic mock` "passed" whenever the still-draining
layer-2-configured process (which needed no probe) answered first. The
gate's only true-probe attempt surfaced as the intermittent 404.

**Fix**: `stop_proc()` helper (terminate → `wait()` reap → TCP-refused
port-free polling) at every terminate site in the layer-3 section; the
section additionally now covers all 5 wrappers (previously 1), falsifying
the old "verified across all 5 wrappers" claim retroactively.

## Verification this round

| Gate | Result |
|------|--------|
| pytest unit | ✅ 325 (316 + 9 R17 resolver tests) |
| compat_layer_e2e | ✅ 5 wrappers × L2 + 5 × 2 layer-3 scenarios, **3/3 back-to-back** |
| runtime_e2e | ✅ 990/990 |
| agent_loop_e2e | ✅ 55/55 |
| multiagent_concurrency | ✅ 10/10 |

## Lesson folded into the harness

A gate that restarts processes must synchronize on the port lifecycle, not
on sleeps — otherwise it tests *whichever process is still alive*. And a
green "auto-discovery" claim deserves an assertion that the dialect was
actually discovered (both directions), not merely that some response
returned 200.
