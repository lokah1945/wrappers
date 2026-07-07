# Bug Analysis: Model Selection - Wrapper-NVIDIA

**Date:** 2026-07-06
**Status:** CONFIRMED BUG - Deep Analysis Complete
**Severity:** MEDIUM-HIGH

---

## Executive Summary

Wrapper-nVIDIA has **60 verified available models** from NVIDIA NIM, but the model selection logic in `resolveTargetModel()` only maps to a **handful of hardcoded candidates**, causing most requests to fall back to `meta/llama-3.1-8b-instruct` regardless of the actual best available model.

---

## Current Behavior

### Metrics (6786 total requests)

| Model | Requests | % Total | Path |
|-------|----------|---------|------|
| meta/llama-3.1-8b-instruct | 3324 | 49% | Both |
| z-ai/glm-5.2 | 1472 | 22% | Both |
| minimaxai/minimax-m3 | 1214 | 18% | Both |
| mistralai/mixtral-8x7b-instruct-v0.1 | 343 | 5% | OpenAI |
| nvidia/nemotron-mini-4b-instruct | 249 | 4% | OpenAI |
| Others (34 models) | 184 | 3% | Mixed |

### Available Models (60 verified OK)

```
High-capability models NEVER used:
- mistralai/mistral-large-3-675b-instruct-2512 (675B params!)
- nvidia/llama-3.3-nemotron-super-49b-v1 (49B)
- nvidia/nemotron-3-super-120b-a12b (120B)
- nvidia/nemotron-3-ultra-550b-a55b (550B)
- qwen/qwen3.5-397b-a17b (397B)
- deepseek-ai/deepseek-v4-pro
- meta/llama-4-maverick-17b-128e-instruct
- google/gemma-4-31b-it
- moonshotai/kimi-k2.6
- openai/gpt-oss-120b
```

---

## Root Cause Analysis

### Primary Issue: `resolveTargetModel()` in `src/index.js:162-262`

The function has **3 critical flaws**:

#### Flaw 1: Hardcoded Mapping Too Limited (Lines 207-224)

```javascript
const mapping = {
  'claude-3-7-sonnet': ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'],
  'claude-3-5-haiku': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
  // ... only ~13 entries total
};
```

**Problem:**
- Only covers Claude and GPT model names
- Missing mappings for newer Claude models (claude-sonnet-4-20250514, claude-3-7-sonnet-latest)
- Missing mappings for Gemini, Llama, Mistral, DeepSeek, Qwen
- Candidate models like `mistralai/mistral-large` don't exist (actual: `mistralai/mistral-large-2-instruct` or `mistralai/mistral-large-3-675b-instruct-2512`)

#### Flaw 2: Heuristic Matching Too Narrow (Lines 229-236)

```javascript
if (lower.includes('sonnet') || lower.includes('opus') || lower.includes('gpt-4')) {
  candidates = ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'];
} else if (lower.includes('haiku') || lower.includes('mini')) {
  candidates = ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'];
}
```

**Problem:**
- Same hardcoded candidates as explicit mapping
- Doesn't consider model capabilities (size, context window, special abilities)
- Doesn't leverage the 60 available models

#### Flaw 3: Default Fallback Too Narrow (Lines 247-258)

```javascript
const preferred = ['meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct', 'meta/llama-3.1-8b-instruct'];
```

**Problem:**
- Only 3 preferred models
- `meta/llama-3.3-70b-instruct` is NOT in the 60 verified available models
- Results in fallback to `meta/llama-3.1-8b-instruct` for most requests

### Secondary Issue: No Capability-Based Selection

The wrapper has a rich `classify()` function in `src/capabilities.js` that understands model capabilities (chat, vision, code, embedding, etc.), but `resolveTargetModel()` doesn't use it.

---

## Impact

1. **Performance**: Users limited to smaller models (8B-70B) when 120B-675B models available
2. **Capability**: Vision, code, and reasoning capabilities not utilized
3. **Cost**: No optimization for cost vs performance tradeoffs
4. **User Experience**: Claude Code requests default to weakest model

---

## Fix Strategy

### Phase 1: Quick Fix - Expand Model Mapping

**File:** `src/index.js` lines 206-262

1. Update hardcoded mapping to use REAL available model IDs
2. Add mappings for all Claude/GPT/Gemini/Llama model variants
3. Update preferred list to use verified available models

### Phase 2: Smart Selection - Capability-Based Routing

**File:** `src/index.js` lines 162-262

1. Use `classify()` to understand requested model capabilities
2. Select best available model based on:
   - Model size (prefer larger models for complex tasks)
   - Context window (match request requirements)
   - Special capabilities (vision, code, reasoning)
   - Availability (check `model_status` table)

### Phase 3: Dynamic Learning

**Future Enhancement:**

1. Track which models work best for different request types
2. Auto-adjust selection based on success rates and latency
3. Consider user preferences and cost constraints

---

## Detailed Fix Plan

### Step 1: Create Model Alias Registry

```javascript
// New file: src/model_aliases.js
const MODEL_ALIASES = {
  // Claude models
  'claude-sonnet-4-20250514': {
    tier: 'large',
    capabilities: ['chat', 'reasoning'],
    preferred: [
      'mistralai/mistral-large-3-675b-instruct-2512',
      'nvidia/nemotron-3-super-120b-a12b',
      'qwen/qwen3.5-397b-a17b',
      'meta/llama-3.3-70b-instruct'
    ]
  },
  'claude-3-7-sonnet': { /* ... */ },
  'claude-3-5-haiku': {
    tier: 'small',
    capabilities: ['chat'],
    preferred: [
      'meta/llama-3.1-8b-instruct',
      'google/gemma-3-4b-it',
      'nvidia/nemotron-mini-4b-instruct'
    ]
  },
  // ... more models
};
```

### Step 2: Update resolveTargetModel()

```javascript
function resolveTargetModel(requestedModel) {
  // 1. Direct match
  if (pool.modelsCached.includes(requestedModel)) return requestedModel;
  
  // 2. Check alias registry
  const alias = MODEL_ALIASES[requestedModel];
  if (alias) {
    for (const cand of alias.preferred) {
      if (pool.modelsCached.includes(cand) && !unavailableModels.has(cand)) {
        return cand;
      }
    }
  }
  
  // 3. Capability-based selection using classify()
  const requestedCaps = classify(requestedModel);
  const candidates = pool.modelsCached
    .filter(m => !unavailableModels.has(m))
    .map(m => ({ id: m, caps: classify(m) }))
    .filter(m => hasRequiredCapabilities(m.caps, requestedCaps))
    .sort((a, b) => b.caps.context_window - a.caps.context_window);
  
  if (candidates.length > 0) return candidates[0].id;
  
  // 4. Default fallback
  return 'meta/llama-3.1-8b-instruct';
}
```

### Step 3: Update Preferred Models List

```javascript
// Verified available models from metrics.db
const VERIFIED_LARGE = [
  'mistralai/mistral-large-3-675b-instruct-2512',
  'nvidia/nemotron-3-super-120b-a12b',
  'nvidia/nemotron-3-ultra-550b-a55b',
  'qwen/qwen3.5-397b-a17b',
  'openai/gpt-oss-120b',
  'meta/llama-3.3-70b-instruct',
  'meta/llama-3.1-70b-instruct'
];

const VERIFIED_SMALL = [
  'meta/llama-3.1-8b-instruct',
  'google/gemma-3-4b-it',
  'nvidia/nemotron-mini-4b-instruct',
  'meta/llama-3.2-3b-instruct',
  'google/gemma-2-2b-it'
];
```

---

## Testing Plan

### Unit Tests

1. Test all Claude model name variations map correctly
2. Test capability-based selection picks larger models
3. Test fallback behavior when preferred models unavailable
4. Test vision model selection for vision requests
5. Test code model selection for code requests

### E2E Tests

1. Send request with `claude-sonnet-4-20250514` → verify uses large model
2. Send request with `claude-3-5-haiku` → verify uses small model
3. Send vision request → verify uses vision-capable model
4. Send code request → verify uses code-optimized model
5. Verify all 60 available models can be selected

### Regression Tests

1. Verify existing functionality preserved
2. Verify no performance degradation
3. Verify error handling unchanged

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaking existing clients | Low | High | Comprehensive test suite |
| New models not available | Medium | Medium | Check model_status table |
| Performance regression | Low | Low | Benchmark before/after |
| Incorrect capability matching | Medium | Medium | Manual verification |

---

## Success Criteria

1. ✅ All Claude model names map to appropriate NVIDIA NIM models
2. ✅ Large requests use 120B+ models when available
3. ✅ Small/fast requests use 8B models
4. ✅ Vision requests use vision-capable models
5. ✅ Code requests use code-optimized models
6. ✅ All 60 verified models can be selected
7. ✅ All existing tests pass
8. ✅ No performance regression

---

## Next Steps

1. **Approve this analysis**
2. **Implement Phase 1 (Quick Fix)** - Update hardcoded mappings
3. **Run comprehensive tests**
4. **Implement Phase 2 (Smart Selection)** - Capability-based routing
5. **Deploy and monitor**

---

## Files to Modify

1. `src/index.js` - `resolveTargetModel()` function
2. `src/capabilities.js` - Add model alias support
3. `test/test.js` - Add model selection tests
4. `src/model_aliases.js` - NEW: Model alias registry

---

## References

- Metrics database: `metrics.db` (6786 requests analyzed)
- Model status: 60 available, 91 unavailable, 151 total
- Source code: `src/index.js:162-262` (resolveTargetModel)
- Capabilities: `src/capabilities.js:252-340` (classify function)

