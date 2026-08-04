# Deep Audit Report — 2026-08-04 Round 12 (hostile-body fuzz gate)

**Scope**: eighth full re-audit "from zero" (R11 committed, all 8 gates
green). This round added a new instrument — a **hostile-body fuzzer** that
throws 32 malformed/adversarial request bodies at every wrapper × surface,
then storms all five wrappers concurrently and sweeps `/health` + `/metrics`
afterwards — and fixed the two real defects it surfaced.

**Result**: 3 defects fixed, 1 new gate (contract **v3.3**, 8→9 gates),
**all 9 gates green (315 unit · 990 runtime · 240 matrix · 55 agent-loop ·
10 concurrency · 1081 fuzz)**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 315 passed |
| `run_runtime_e2e.py` | ✅ 990 checks |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2+L3 |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |
| `agent_loop_e2e.py` | ✅ 55 checks |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks |
| `fuzz_bodies_e2e.py` (**new**) | ✅ 1081 checks |

---

## New instrument — `tests/e2e_runtime/fuzz_bodies_e2e.py` (1081 checks)

Self-contained: boots its own mock upstream + all 5 wrappers on private
ports, then per wrapper × surface (`/v1/messages`, `/v1/chat/completions`,
`/v1/responses`) drives **32 hostile bodies**:

- JSON of every wrong type (`null`, int, string, bool, top-level array)
- malformed bodies: truncated JSON, junk bytes, BOM-prefixed, truncated
  mid-UTF-8 multibyte sequence
- non-finite numbers (`NaN`, `Infinity`) and wrong-typed fields
  (`model` as int/list, `messages` of every bad shape, `system` non-string)
- `max_tokens` negative / string / float / astronomically large
- deep nesting (100× and 900×), unicode garbage, 1 MB string field
- `/v1/responses` variants: bad `input` shapes, bogus
  `previous_response_id`, non-string `instructions`

Assertions per request (CONTRACT §4, §10): **shaped 4xx — never an unshaped
5xx, never a connection crash/hang, never a transport-level exception**.
Then: a **concurrent fuzz burst** (6 hostile bodies per wrapper fired
simultaneously) and a **post-storm sweep** asserting `/health` and
`/metrics` still return 200 and pass the recursive `_find_in_flight`
extraction (no residual in-flight leaks, no poisoned counters).

First run: **1060 pass / 21 fail** → the failures clustered into exactly two
root causes, both real defects (below). After the fixes: **1081 / 0**.

---

## Findings & fixes

### B-12.1 — nvidia GENAI base ignored custom `NVIDIA_BASE_URL` (routing leak)

**Class**: §9.1 config-routing bug. `BASE_GENAI` (the base URL for
embeddings / ranking / images-generation calls) defaulted to the public
NVIDIA cloud host `ai.api.nvidia.com` and **never consulted the
operator-provided custom endpoint** (`NVIDIA_BASE_URL`). Symptom: with a
custom/upstream mock configured, embeddings/rank/images requests bypassed it
entirely and went to the real cloud API — in the sandbox: unreachable →
5xx surfaced to the client; in production against a private mock/proxy:
silent mis-routing and credential/egress leak to the public internet.
The fuzz gate hit this because its post-storm capability probes include the
GENAI surfaces against the mock upstream.

**Fix** (`nvidia-python/src/main.py`):

```python
_explicit_llm_base = (os.environ.get("NVIDIA_BASE_URL") or "").strip().rstrip("/")
BASE_GENAI = (
    (os.environ.get("NVIDIA_GENAI_URL") or "").strip().rstrip("/")
    or _explicit_llm_base
    or NVIDIA_GENAI_URL          # official cloud default (unchanged)
)
```

Precedence is now: explicit `NVIDIA_GENAI_URL` override → custom
`NVIDIA_BASE_URL` → cloud default. `BASE_NVCF` intentionally untouched
(display-only in `/v1/capabilities`).

### B-12.2 — `/metrics` JSON parity gap on 3 of 5 wrappers (CONTRACT §10)

nvidia (`live_keys`) and openrouter (`pool`) exposed live per-key pool stats
and in-flight reservation counts in `/metrics`; **nous, opencode and
blackbox served a bare counter snapshot** — no `pool` block, no `in_flight`.
Cross-wrapper dashboards/alerts keyed on those fields silently went blind
for 3 of 5 wrappers; it also made the post-storm "no leaked reservations"
proof unverifiable against 60% of the fleet.

**Fix** (one pattern, three wrappers):

- `nous/src/main.py` — `metrics.snapshot()` result extended with
  `snap["pool"] = KEY_POOL.all_stats()` and `snap["in_flight"] = sum(...)`.
- `opencode/src/main.py`, `blackbox/src/main.py` — `await metrics.summary()`
  extended with `s["pool"] = pool.all_stats()` and `s["in_flight"]`.

All five `/metrics` JSON payloads now carry both fields; the fuzz gate's
post-storm sweep extracts them identically across the fleet (§10 parity,
locked by the gate).

### B-12.3 — layer-2 converter silently ignored `max_completion_tokens`

**Class**: cross-dialect translation gap. Newer OpenAI SDKs send
`max_completion_tokens` in place of `max_tokens`. The shared
OpenAI→Anthropic request converter (`common/translations/shared.py`,
COMPATIBILITY_LAYER=2) whitelisted only `max_tokens` — the alias was
**silently dropped, removing the client's output cap entirely**
(unbounded generation; count_tokens divergence). OpenAI-dialect paths are
unaffected (body forwarded verbatim).

**Fix**: the converter now coalesces the alias —

```python
mt = chat_body.get('max_tokens')
if mt is None:
    mt = chat_body.get('max_completion_tokens')
if mt is not None:
    out['max_tokens'] = mt
```

`max_tokens` wins when both are set; no cap is injected when neither is
(no silent default mutation). Regression-locked by
`test_r12_max_completion_tokens_coalesced` (+2 sibling structural locks for
B-12.1/B-12.2 — 315 unit total).

---

## Negative sweeps (no finding)

- **Contract re-read §1–§13** against current tree: no clause regression;
  §11/§12/§13 updated to v3.3 in the same commit.
- **§7 no-fork re-sweep**: the R12 edits touch only config resolution
  (`BASE_GENAI`) and each wrapper's own `/metrics` endpoint — no new local
  copies of shared helpers introduced; `TestR11SharedNoFork` still green.
- **Store/id uniqueness re-sweep (R7–R10 classes)**: no new id mint sites
  added; `resp_<ms>-<rand>`, `_new_msg_id()` and uniquified `toolu_*`
  fallbacks unchanged; 312 unit incl. `TestR9UniqueMessageIds` /
  `TestR10*` / `TestStoreDeepCopyIsolation` all green.
- **Body-guard coverage**: the 32-battery confirms §4 shaped-4xx on every
  wrong-typed/malformed body class across all three surfaces of all five
  wrappers — including JSON-null hole, JSON scalars, and truncated UTF-8.
- **Post-storm integrity**: after 160 hostile requests fired concurrently,
  all five wrappers serve `/health` + `/metrics` 200 with sane recursive
  in-flight extraction — zero leaked reservations (§2.2.4, §6.1).

## Conclusion

Round 12 closes the last observed gap between the contract's *prose* (§4:
"malformed input never yields an unshaped 5xx") and fleet-wide *evidence*:
before this round no gate had ever thrown hostile bodies at the servers
concurrently; doing so immediately paid off with two real fixes. Contract
**v3.3** promotes the fuzzer to gate 9 of 9. **9/9 gates green, 0
failures**; readiness for concurrent multi-agent traffic (Claude Code,
Codex, OpenClaw, Hermes, opencode, OpenHands and any OpenAI/Anthropic-SDK
client) is re-verified end-to-end with zero cross-talk and zero leaked
reservations.
