# BUG REPORT: mistral-large-4-1t-a52b context window reported as 262K instead of 1M

- **Report ID:** BUG-20261102-MISTRAL-LARGE-4-CONTEXT-001
- **Date:** 2026-11-02
- **Reporter:** ILMA (Hermes agent) — root cause analysis only, NO files edited
- **Project:** `/root/wrapper` (wrappers repo, branch from github.com/lokah1945/wrappers)
- **Affected model:** `mistralai/mistral-large-4-1t-a52b`
- **Severity:** HIGH — all clients reading `context_window` from `/v1/models` get wrong value (262,144 instead of 1,048,576), causing premature compaction/truncation of prompts between 262K–1M tokens
- **Status:** ❌ NOT FIXED — awaiting agent execution

---

## 1. SUMMARY

The NVIDIA wrapper reports `context_window: 262144` for `mistralai/mistral-large-4-1t-a52b` in `/v1/models`, but live API probes prove the actual limit is **1,048,576 (1M) tokens**. Three compounding bugs cause this: (A) wrong source field parsed from NVIDIA's self-contradictory build page, (B) context probe trapped in "verify mode" that never probes above the advertised value, (C) error parser doesn't recognize NVIDIA's `details.type=context_length` rejection format.

---

## 2. EVIDENCE (live probes against integrate.api.nvidia.com)

All probes used NVIDIA API key from `/root/wrapper/nvidia-python/.env` (key1), model `mistralai/mistral-large-4-1t-a52b`:

| Test | Prompt chars | Result | prompt_tokens reported |
|------|-------------|--------|----------------------|
| 1 | 1,200,000 | ✅ ACCEPTED | 180,021 |
| 2 | 1,810,000 | ✅ ACCEPTED | 291,957 (**> 262,144 — proves 262K is wrong**) |
| 3 | 2,400,000 | ✅ ACCEPTED | 360,021 |
| 4 | 6,000,000 | ✅ ACCEPTED | 967,763 |
| 5 | 7,000,000 | ❌ REJECTED HTTP 400 | `{"details":{"type":"context_length","limit":1048576,"actual":1129054}}` |

**Conclusion:** exact context limit = **1,048,576 tokens** (the rejection explicitly states `limit: 1048576`).

Current wrapper output (port 9101):
```json
"context_metadata": {
  "context_window": 262144,
  "source": "https://build.nvidia.com/mistralai/mistral-large-4-1t-a52b/build",
  "probe": { "verified_context_window": 262155 }
}
```

---

## 3. ROOT CAUSE — 3 compounding bugs

### BUG A — Wrong field parsed from self-contradictory NVIDIA build page

| Field | File | Line | Status |
|-------|------|------|--------|
| `parse_build_context()` reads `spec.attributes.specifications.contextLength` | `/root/wrapper/common/nvidia_model_specs.py` | 92–132 (read at line 127) | ❌ NOT FIXED |

**Problem:** NVIDIA's own build page `https://build.nvidia.com/mistralai/mistral-large-4-1t-a52b/build` contains contradictory statements:
1. `"specifications":{"contextLength":262144,...}` — machine-readable spec field (what the parser reads)
2. `"max_tokens":{"maximum":262144,...}` with description "combined prompt and generated response must fit within the 262,144-token context window"
3. Prose: *"Text and images share a combined input/output context window of up to **1M tokens**"* and *"Combined input and output context length is **1M tokens**"*

The parser trusts field (1) over prose (3). Live API proves (3) is correct — the 262144 is actually the **max output tokens** cap duplicated into `contextLength`, not the input context window.

**Data flow:** `parse_build_context` → `ngc-featured-cache.json` (entry has `"context": 262144`) → `registry.get_official_context()` → `/v1/models` `context_window: 262144`.

**Note:** `mistral-large-4` is NOT in NGC featured-models.json (only 4 models there: nemotron-3-ultra, nemotron-3.5-super-vl, glm-5-3, deepseek-v4.1-flash — all with context=1048576). So the build page is the only source for this model.

**Fix approach (for executing agent):** In `parse_build_context`, also scan the prose/description fields in the spec attributes for patterns like `(\d+)M tokens` / `(\d{1,3}(?:,\d{3})+)` near "context window" and if the machine-readable `contextLength` conflicts with a larger prose value, prefer the prose OR mark as conflict and let the probe resolve it. Minimum viable fix: parse both `contextLength` and prose mentions; if they disagree, return the larger value (or None to force probing).

---

### BUG B — Context probe trapped in "verify_mode", never probes above advertised value

| Field | File | Line | Status |
|-------|------|------|--------|
| `_probe_model()` verify_mode logic | `/root/wrapper/common/model/context_probe.py` | 265–294 | ❌ NOT FIXED |

**Code:**
```python
if positive_int(advertised):
    eligible = [r for r in rungs if r <= advertised]
    if eligible:
        rungs = [eligible[-1]]   # only probe rung <= advertised (262144)
        verify_mode = True
...
if verify_mode:
    result.outcome = 'advertised_verified'
    break   # stops after ONE accepted rung — never tries 524288 or 1048576
```

**Problem:** When a published/advertised context exists (even wrong), the probe only tests at-or-below that value and stops on first success. Log evidence: `mistralai/mistral-large-4-1t-a52b recorded floor=262155 window=None` — recorded floor 262155 but never discovered the real 1M window.

**Fix approach:** In verify mode, after accepting the advertised rung, continue to the NEXT rung above it. If that also accepts, keep climbing. Only stop on rejection (which reveals exact window via Bug C fix) or ladder top. Alternatively: always probe one rung above advertised to verify it's not under-reported.

---

### BUG C — `parse_context_error` doesn't recognize NVIDIA's rejection format

| Field | File | Line | Status |
|-------|------|------|--------|
| `parse_context_error()` | `/root/wrapper/common/context_recovery.py` | 44–110 | ❌ NOT FIXED |

**Problem:** NVIDIA returns context errors as:
```json
{"message":"Invalid request","type":"Bad Request","code":400,
 "details":{"type":"context_length","limit":1048576,"actual":1129054}}
```
The parser checks `error.get('detail')` (singular, for FastAPI/Pydantic), regex on message text, and `detail.get('context_length')`. It never checks `error['details']['type'] == 'context_length'` + `error['details']['limit']`. Verified: `parse_context_error(400, nvidia_err)` returns `None` for all shapes (dict, json string, nested).

**Impact:** When a user sends >1M tokens through the wrapper, the recovery system can't extract the exact limit, so it can't do bounded recovery (compact_input / set_explicit_output_limit).

**Fix approach:** Add handling in `parse_context_error`: check `error.get('details')` dict for `type == 'context_length'`, extract `limit` as `context_window` and `actual` as `input_tokens`. Insert near line 105 where `context = context or positive_int(detail.get(...))` — add `or positive_int((error.get('details') or {}).get('limit'))` when `(error.get('details') or {}).get('type') == 'context_length'`.

---

## 4. AFFECTED FILES (complete list for executing agent)

| # | File | Bug | Action needed |
|---|------|-----|---------------|
| 1 | `/root/wrapper/common/nvidia_model_specs.py` | A | Fix `parse_build_context` to handle conflicting spec vs prose values |
| 2 | `/root/wrapper/common/model/context_probe.py` | B | Fix `_probe_model` verify_mode to probe above advertised value |
| 3 | `/root/wrapper/common/context_recovery.py` | C | Add NVIDIA `details.type=context_length` parsing to `parse_context_error` |
| 4 | `/root/wrapper/nvidia-python/ngc-featured-cache.json` | A (stale cache) | Delete or invalidate the `mistralai/mistral-large-4-1t-a52b` entry so it re-fetches; or the registry will overwrite on next refresh after fix A |
| 5 | `/root/wrapper/model-state.db` | B (stale probe) | `model_context_observations` table is empty for this model; after fix B, the probe will re-record. May need to clear `_skip_until` in-memory state by restarting wrapper-nvidia-python.service |

**Callers/dependents to check after fix:**
- `/root/wrapper/nvidia-python/src/main.py` lines 2186–2210 (`_context_catalog_entry`, `merge_observed_context`) — reads `registry.get_official_context()`
- `/root/wrapper/nvidia-python/src/registry.py` line 243 (`get_official_context`) — consumer of cache
- `/root/wrapper/common/model/metadata.py` (`merge_context_metadata`, `merge_observed_context`, `discovery_limits`) — merges published + observed
- Tests: `/root/wrapper/tests/test_context_probe.py`, `/root/wrapper/tests/test_context_metadata.py` — may need new cases for NVIDIA details format

---

## 5. VERIFICATION STEPS (after fix)

```bash
# 1. Restart wrapper to pick up code changes
sudo systemctl restart wrapper-nvidia-python.service

# 2. Force re-parse of build page (delete stale cache entry or wait for TTL)
#    NGC_FEATURED cache TTL is REGISTRY_REFRESH_SEC=3600; build page TTL is 86400
#    Fastest: delete the mistral entry from ngc-featured-cache.json and restart

# 3. Check /v1/models reports 1048576
curl -s http://127.0.0.1:9101/v1/models | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d['data']:
    if 'mistral-large-4' in m['id']:
        print('context_window:', m.get('context_window'))
        print('context_metadata:', json.dumps(m.get('context_metadata',{}), indent=2))
"

# 4. Verify probe discovers 1M (check logs)
grep 'mistral-large-4' /root/wrapper/nvidia-python/nvidia_py.log | grep context-probe | tail -5
# Expected: 'recorded floor=... window=1048576' or 'advertised_verified' with higher rung

# 5. Verify error parsing
python3 -c "
import sys; sys.path.insert(0,'/root/wrapper')
from common.context_recovery import parse_context_error
err={'details':{'type':'context_length','limit':1048576,'actual':1129054}}
r=parse_context_error(400,err)
assert r and r.context_window==1048576, f'FAIL: {r}'
print('PASS: context_window=', r.context_window)
"
```

---

## 6. SCOPE / RISK NOTES

- **Other models affected?** Likely yes — any model whose context comes only from build.nvidia.com page (not NGC featured-models.json) may have the same issue if NVIDIA's page has the same 262144-in-spec vs larger-in-prose pattern. Recommend running the fixed probe across all build-page-sourced models.
- **Not checked:** Whether other providers (nous, openrouter) have similar context misreporting — out of scope for this report.
- **Cache invalidation:** `ngc-featured-cache.json` has `expires_at` per entry; mistral entry expires at `1793632601` (~2026-11-03). After fix A is deployed, either wait for expiry or manually remove the entry.
- **Pending providers frozen:** `blackbox/` and `opencode/` are in pending status per `wrappers.json` — do NOT touch them.

---

## 7. PROGRESS TRACKER

| Bug | ID | File | Status | Fixed by | Date | Verified |
|-----|-----|------|--------|----------|------|----------|
| A | BUG-A-PARSE-BUILD-CONTEXT | `common/nvidia_model_specs.py` | ❌ NOT FIXED | — | — | — |
| B | BUG-B-PROBE-VERIFY-MODE | `common/model/context_probe.py` | ❌ NOT FIXED | — | — | — |
| C | BUG-C-NVIDIA-ERROR-FORMAT | `common/context_recovery.py` | ❌ NOT FIXED | — | — | — |
| Cache | CACHE-INVALIDATE | `nvidia-python/ngc-featured-cache.json` | ❌ NOT FIXED | — | — | — |

**Instructions for executing agent:** Fix bugs A, B, C in order. After each fix, update the Status column above to ✅ FIXED with your name/date. Run verification steps in section 5 after all fixes. Commit to the wrappers repo (`/root/wrapper`, remote `origin` = local wrapper_remote.git per memory note, or GitHub lokah1945/wrappers — verify `git remote -v` first).

---

*Report generated by ILMA v3.31 — 2026-11-02 — root cause only, no files modified during analysis*
