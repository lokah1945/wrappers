# EXECUTION PLAN: BUG-20261102-MISTRAL-LARGE-4-CONTEXT-001

**Plan ID:** PLAN-BUG-20261102-MISTRAL-LARGE-4-CONTEXT-001
**Date:** 2026-11-03
**Status:** ✅ COMPLETED
**Based on:** BUG-20261102-MISTRAL-LARGE-4-CONTEXT-001.md

---

## OBJECTIVE

Fix three compounding bugs causing NVIDIA wrapper to report `context_window: 262144` instead of `1048576` for `mistralai/mistral-large-4-1t-a52b`, **and establish patterns that prevent similar issues for other models sourced from build.nvidia.com**.

---

## ROOT CAUSE ANALYSIS (Expanded)

### BUG A — `parse_build_context` (common/nvidia_model_specs.py:92-131)

**Current flow:**
1. Fetches build page HTML → extracts Flight-format JSON from `<script>` tags
2. Parses `_flight_records` to find spec object with matching `artifactName`
3. Reads `spec.attributes.specifications.contextLength` → returns single value
4. **Critical flaw (line 130):** "Conflicting specifications are uncertain, never pick the largest one" → returns `None` if multiple values found

**NVIDIA build page contradiction:**
- Machine-readable: `"specifications":{"contextLength":262144}`
- Prose/description: *"Text and images share a combined input/output context window of up to **1M tokens**"* and *"Combined input and output context length is **1M tokens**"*

**Why other models are at risk:**
- Any model NOT in NGC `featured-models.json` (only 4 models: nemotron-3-ultra, nemotron-3.5-super-vl, glm-5-3, deepseek-v4.1-flash) relies on build page
- NVIDIA's build pages may have same pattern: spec `contextLength` = max output tokens, prose = actual context window

---

### BUG B — `_probe_model` verify_mode (common/model/context_probe.py:265-294)

**Current logic (lines 266-294):**
```python
if positive_int(advertised):
    eligible = [r for r in rungs if r <= advertised]
    if eligible:
        rungs = [eligible[-1]]   # Only probe rung <= advertised (262144)
        verify_mode = True
...
if verify_mode:
    result.outcome = 'advertised_verified'
    break   # Stops after ONE accepted rung — never tries 524288 or 1048576
```

**Result:** Log shows `mistralai/mistral-large-4-1t-a52b recorded floor=262155 window=None` — never discovered real 1M window.

**Why other models are at risk:**
- Any model with under-reported advertised context gets stuck at wrong floor
- The probe design assumes advertised value is correct ceiling

---

### BUG C — `parse_context_error` (common/context_recovery.py:44-108)

**NVIDIA error format (not handled):**
```json
{"message":"Invalid request","type":"Bad Request","code":400,
 "details":{"type":"context_length","limit":1048576,"actual":1129054}}
```

**Current parser checks:**
- `error.get('detail')` — FastAPI/Pydantic validation errors
- Regex on message text
- `detail.get('context_length')` / `detail.get('max_context_length')`
- **Missing:** `error['details']['type'] == 'context_length'` + `error['details']['limit']`

**Why other models are at risk:**
- NVIDIA uses this format across models; without parsing, rejection evidence is lost
- Probe can't get exact window from rejection → probe stops at floor only

---

## AFFECTED MODELS ASSESSMENT

### Models relying on build.nvidia.com (NOT in NGC featured-models.json):
| Model | Risk Level | Reason |
|-------|------------|--------|
| `mistralai/mistral-large-4-1t-a52b` | **CONFIRMED BROKEN** | Proven 1M vs 262K discrepancy |
| Other Mistral models on build.nvidia.com | **HIGH** | Same publisher, likely same page structure |
| Models from other publishers on build.nvidia.com | **MEDIUM** | May have similar spec vs prose conflict |
| Any model NOT in NGC featured-models.json | **MEDIUM** | Solely relies on build page parsing |

### Models in NGC featured-models.json (SAFE):
- `nvidia/nemotron-3-ultra-550b-a55b` — context: 1048576
- `nemotron-3-super-120b-a12b` — context: 1048576
- `z-ai/glm-5-3` — context: 1048576
- `deepseek-ai/deepseek-v4.1-flash` — context: 1048576

---

## COMPREHENSIVE FIX STRATEGY

### Phase 1: Bug A — Robust `parse_build_context` with Prose Parsing

**File:** `/root/wrapper/common/nvidia_model_specs.py`

**Changes:**
1. **Add prose extraction** from Flight records — scan `attributes.description`, `attributes.specsDescription`, `attributes.longDescription`, and any text fields for:
   - Pattern `(\d+)M\s*tokens?` → multiply by 1,000,000
   - Pattern `(\d{1,3}(?:,\d{3})+)\s*tokens?` near "context window"
   - Pattern `(\d+)K\s*tokens?` → multiply by 1,000 (but flag as likely rounded)
2. **Conflict resolution strategy:**
   - If spec `contextLength` AND prose value exist AND prose > spec × 2:
     - **Return prose value** (live API proves prose is correct for mistral-large-4)
     - Log conflict for audit: `WARNING: build page spec contextLength={spec} conflicts with prose={prose} for {model_id}`
   - If only spec value: return spec (current behavior)
   - If only prose value: return prose (new capability)
   - If multiple conflicting values: return max (not None — don't lose all data)
3. **Preserve backward compatibility:** Return single int or None; don't change function signature

**Test additions** (test_nvidia_model_specs.py):
- `test_prose_context_override_spec`: prose "1M tokens" overrides spec 262144
- `test_prose_context_m_parsing`: "1M tokens" → 1048576
- `test_prose_context_k_parsing`: "128K tokens" → 131072 (flagged as rounded)
- `test_conflict_logging`: verify warning logged when spec vs prose differ significantly

---

### Phase 2: Bug B — Enhanced Verify Mode with Ceiling Discovery

**File:** `/root/wrapper/common/model/context_probe.py`

**Changes to `_probe_model` (lines 265-312):**

**Option A (Minimal — probe one rung above):**
```python
if verify_mode:
    # After accepting advertised rung, probe NEXT rung to check for under-report
    result.outcome = 'advertised_verified'
    # NEW: Continue to next rung if available
    current_idx = self.ladder.index(target)
    if current_idx + 1 < len(self.ladder):
        next_target = self.ladder[current_idx + 1]
        # Try one more rung — if accepted, keep climbing; if rejected, we get exact window
        continue  # Don't break, let loop continue
    break
```

**Option B (Robust — full ladder climb in verify mode):**
```python
if verify_mode:
    # Advertised verified, but continue climbing to discover true ceiling
    result.outcome = 'advertised_verified'
    # Don't break — continue loop to test higher rungs
    # If higher rung accepted: update verified_floor, keep going
    # If higher rung rejected with context error: set context_window, break
    continue
```

**Recommendation:** Option B — more thorough, aligns with "reinforcement" design contract

**Key logic change:**
- In verify_mode, after first accepted rung, continue to next rung(s)
- Track highest accepted rung as `verified_floor`
- Stop on: context rejection (sets exact `context_window`), ladder top, or non-context error
- Outcome: `'advertised_verified'` if no higher rung tested, `'verified_extended'` if climbed higher

**Test additions** (test_context_probe.py):
- `test_verify_mode_climbs_above_advertised`: advertised=262144, accepts 524288, rejects 1048576 with context_window=1048576
- `test_verify_mode_stops_at_non_context_error`: advertised=262144, next rung returns 500 → stops, records floor only
- `test_verify_mode_full_ladder`: unknown advertised, climbs full ladder

---

### Phase 3: Bug C — NVIDIA Error Format Support

**File:** `/root/wrapper/common/context_recovery.py`

**Changes to `parse_context_error` (around line 105):**

```python
# Add after line 104 (before definite check)
# NVIDIA-specific: check details.type == 'context_length'
details = error.get('details') if isinstance(error, dict) else None
if isinstance(details, dict) and details.get('type') == 'context_length':
    # Extract exact window from rejection
    context = context or positive_int(details.get('limit'))
    input_tokens = input_tokens or positive_int(details.get('actual'))
    # This IS a definite context error — mark as such
    definite = True
```

**Also handle nested in `error.get('error', {}).get('details')`** for consistency.

**Test additions** (test_context_recovery.py):
- `test_nvidia_details_context_length`: error with `details.type=context_length`, `limit=1048576`
- `test_nvidia_nested_error_details`: error with `error.details.type=context_length`

---

### Phase 4: Cross-Model Validation & Cache Invalidation

**Files to handle:**
1. `/root/wrapper/nvidia-python/ngc-featured-cache.json` — Remove mistral entry OR wait for TTL expiry
2. `/root/wrapper/model-state.db` — `model_context_observations` table will auto-populate after probe runs

**Validation script to run after fixes:**
```bash
# Test all build-page-sourced models
python3 -c "
import sys; sys.path.insert(0,'/root/wrapper')
from common.nvidia_model_specs import parse_build_context
from nvidia_python.src.registry import Registry
# List models that use build page (not NGC featured)
# Run parse_build_context on their pages (mock or live)
# Verify no model returns 262144 when prose indicates higher
"
```

---

## IMPLEMENTATION ORDER

| Step | Bug | File | Estimated Effort |
|------|-----|------|------------------|
| 1 | A | `common/nvidia_model_specs.py` | Medium |
| 2 | B | `common/model/context_probe.py` | Medium |
| 3 | C | `common/context_recovery.py` | Low |
| 4 | Tests | `tests/test_nvidia_model_specs.py` | Medium |
| 5 | Tests | `tests/test_context_probe.py` | Medium |
| 6 | Tests | `tests/test_context_recovery.py` | Low |
| 7 | Cache | `nvidia-python/ngc-featured-cache.json` | Trivial |
| 8 | Verify | Full integration test | Medium |

---

## VERIFICATION STEPS (Post-Fix)

### Step 1: Restart & Cache Clear
```bash
# Clear stale cache entry for mistral
python3 -c "
import json
with open('/root/wrapper/nvidia-python/ngc-featured-cache.json') as f:
    data = json.load(f)
data['map'] = {k:v for k,v in data['map'].items() if 'mistral-large-4' not in k}
with open('/root/wrapper/nvidia-python/ngc-featured-cache.json', 'w') as f:
    json.dump(data, f, indent=2)
print('Cleared mistral cache entry')
"

sudo systemctl restart wrapper-nvidia-python.service
```

### Step 2: Verify `/v1/models` Reports 1048576
```bash
curl -s http://127.0.0.1:9101/v1/models | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d['data']:
    if 'mistral-large-4' in m['id']:
        print('context_window:', m.get('context_window'))
        print('context_metadata:', json.dumps(m.get('context_metadata',{}), indent=2))
"
# EXPECTED: context_window: 1048576
```

### Step 3: Verify Probe Discovers 1M Ceiling
```bash
grep 'mistral-large-4' /root/wrapper/nvidia-python/nvidia_py.log | grep context-probe | tail -10
# EXPECTED: 'recorded floor=... window=1048576' or 'advertised_verified' with higher rung
```

### Step 4: Verify Error Parsing
```bash
python3 -c "
import sys; sys.path.insert(0,'/root/wrapper')
from common.context_recovery import parse_context_error
err={'details':{'type':'context_length','limit':1048576,'actual':1129054}}
r=parse_context_error(400,err)
assert r and r.context_window==1048576, f'FAIL: {r}'
print('PASS: context_window=', r.context_window)
"
```

### Step 5: Run Full Test Suite
```bash
cd /root/wrapper && python3 -m pytest tests/test_nvidia_model_specs.py tests/test_context_probe.py tests/test_context_recovery.py -v
```

### Step 6: Cross-Model Spot Check
```bash
# Test other build-page models (manual spot check)
curl -s http://127.0.0.1:9101/v1/models | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d['data']:
    meta = m.get('context_metadata',{})
    src = meta.get('source','')
    if 'build.nvidia.com' in src:
        print(m['id'], '->', m.get('context_window'), 'source:', src)
"
```

---

## PROGRESS TRACKER

| Bug | ID | File | Status | Fixed By | Date | Verified |
|-----|-----|------|--------|----------|------|----------|
| A | BUG-A-PARSE-BUILD-CONTEXT | `common/nvidia_model_specs.py` | ✅ FIXED | Agent | 2026-11-03 | ✅ |
| B | BUG-B-PROBE-VERIFY-MODE | `common/model/context_probe.py` | ✅ FIXED | Agent | 2026-11-03 | ✅ |
| C | BUG-C-NVIDIA-ERROR-FORMAT | `common/context_recovery.py` | ✅ FIXED | Agent | 2026-11-03 | ✅ |
| Tests | TEST-COVERAGE | `tests/test_*.py` | ✅ FIXED | Agent | 2026-11-03 | ✅ |
| Cache | CACHE-INVALIDATE | `nvidia-python/ngc-featured-cache.json` | ⏳ MANUAL | — | — | — |

---

## RISK MITIGATION

| Risk | Mitigation |
|------|------------|
| Prose parsing picks up unrelated "1M tokens" text | Only scan known description fields; require "context window" proximity |
| Verify mode increases probe load | Limited by `CONTEXT_PROBE_MAX_PER_CYCLE=16`, `CONTEXT_PROBE_CYCLE_SEC=240` |
| NVIDIA error format changes | Parser is additive — won't break existing formats |
| Other models have different build page structure | Log conflicts; fallback to spec value if prose parsing fails |
| Cache TTL delays fix visibility | Manual cache clear in verification step |

---

## DESIGN PRINCIPLES (Per Owner Instructions)

1. **Honesty**: Probe records FLOOR (`verified_context_window`), never ceiling — exact window only from rejection
2. **Published wins**: Catalog values never overridden by probe; probe only fills unknowns
3. **No over-labeling**: Small-context models never get inflated limits
4. **Minimal changes**: Fix bugs without redesigning architecture
5. **Test coverage**: Every fix must have regression test

---

*Plan generated for execution agent — 2026-11-03*