# Deep Audit Report — 2026-08-04 Round 11 (CONTRACT §7 no-fork closure)

**Scope**: seventh full re-audit "from zero" (R10 committed, all 8 gates
green). This round walked the contract clause by clause; §7 ("shared modules:
single implementation, prose-only differences, no shadowing/forking of shared
helpers") had two live violations.

**Result**: 2 §7 forks eliminated, 2 regression tests added, nous inlined
state machine FULLY re-verified at parity, **all 8 gates green (312 unit)**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 312 passed |
| `run_runtime_e2e.py` | ✅ 990 checks |
| `agent_loop_e2e.py` | ✅ 55 checks |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2+L3 |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |

---

## Findings & fixes

### R11.1 — nous `repair_orphan_tool_messages` was a drifted §7 fork

nous imported the shared helper (`_repair_orphan_tool_messages`, line 74)
**and still called its own local copy** (1052) at the replay site (1114). The
copies had drifted: the shared version joins list-typed tool `content` blocks
into text; the nous copy did `f"...{m.get('content')}"` — a structured tool
result arrived upstream as **raw stringified JSON**
(`"Tool result for X: [{'type': 'text', ...}]"`), degrading every orphan
repair from nous (orphan repair is the R6 orphan-tool-call safety net).

**Fix**: the wrapper now delegates to the shared implementation when
`common.translations` is importable (production); the local body survives
only in the documented ImportError fallback path — and was corrected to match
shared semantics byte-for-byte there too.

### R11.2 — nvidia `_parse_dsml_from_text` manually-synced §7 twin

nvidia main kept a hand-synchronised copy of the shared MiniMax DSML parser
(it already imported `_shared_parse_dsml`). Hand-synchronised twins are the
exact defect class §7 exists to prevent: round-to-round fixes (R5 incomplete-
markup suppression, R10 id uniqueness) had to be applied twice, and any
future fix landing only on one side silently splits behaviour between nvidia
and its four siblings.

**Fix**: `_parse_dsml_from_text` delegates to the shared parser when
importable; the local body remains only as the documented ImportError
fallback.

**Regression locks** (`TestR11SharedNoFork`): nous output equals
`shared.repair_orphan_tool_messages` on assistant+orphan-tool+paired-tool
input (list content joined, paired tools preserved); nvidia DSML parser
output equals `shared.parse_dsml_from_text` on complete markup, and still
suppresses incomplete markup in the fallback path.

## Parity deep-check (documented deviation re-verified)

The contract's one accepted deviation — nous' inlined dict-based
`AnthropicStreamState` — was re-read in full against
`common/translations/anthropic_stream.py`: R-02 parallel tool blocks stay
open (`open_tool_blocks` + `_close_nontool_block`), P3-4 reasoning blips
don't orphan tool arguments, P0-4 filter flush into own channel, R5 DSML
drain + `end_turn→tool_use` upgrade, B-06 strict `finish_reason→stop_reason`
mapping, R-03 error frame with `stop_reason=None`, R9/R10 unique msg/tool
ids. **Verified at parity; no drift.**

## Negative sweeps (clean this round)

| Area | Verdict |
|------|---------|
| Remaining `filter_special_tokens`/`parse_dsml_from_text` defs in wrappers | ✅ all are guarded ImportError fallbacks only (`# type: ignore[misc]` arms) |
| `shared.py` remainder (retry-after parsing incl. RFC-1123 dates, B-21 cooldown policy with anti-bot + model-capacity carve-outs, header-forward allowlists) | ✅ single canonical implementation |
| nvidia pool model-timestamp dicts (`_model_ts`, `_model_ts_by_key`) | ✅ 60s-window pruned on each write; keys bounded by catalog |
| nous finish-order (tool-stop before text-stop) vs shared | ✅ both valid event sequences |

**Verification at commit: 8/8 gates green — 312 unit · 990 runtime E2E · 240
matrix · SDK-compat · L2+L3 · 55 agent-loop · 10 concurrency · soak.**
