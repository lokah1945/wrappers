# AUDIT: NO MODEL FALLBACK Principle — 2026-07-29

**Scope:** `/home/z/my-project/repos/wrappers/{nvidia-python, nous, opencode, blackbox, openrouter}`
**Authority:** user's exact words — "tidak ada fallback model"; "jika model A request maka diteruskan juga ke model A"; "retry dilakukan ke key lain untuk loadbalancing"; "sampai semua key menunjukkan error yang sama baru kemudian error diteruskan ke client"; "Tidak boleh client request dengan model A, tapi di fallback ke model B, ini kesalahan konsep fatal"; "Error model A di key1 maka retry model A ke key lainnya. Dan model B masih boleh pakai key 1 karena berbeda".
**Verdict per wrapper:** see summary table at bottom.

---

## 1. nvidia-python (`src/main.py`, `key_pool.py`, `anthropic_compat.py`, `responses_compat.py`, `registry.py`, `capabilities.py`)

### A. Model substitution / fallback violations
**No silent model substitution found.** Several *informational* substitution-shaped constructs exist but none of them mutate the request body upstream.

1. `DEPRECATED_MODEL_REDIRECTS` — `main.py:781-793` — hardcoded map (`minimaxai/minimax-m2.5` → `minimaxai/minimax-m2.7`, `z-ai/glm-5.1` → `z-ai/glm-5.2`, `nvidia/llama-3.3-nemotron-super-49b` → `…-49b-v1.5`, etc.). Used ONLY by `get_deprecated_redirect_info` (`main.py:962-968`), and that function returns `None` unless `DEPRECATED_MODEL_REDIRECT_ERROR=1`. When enabled, the wrapper returns `410 Gone` with a message telling the client to update its request — **it does NOT silently rewrite the model id**. Call sites: `main.py:2024-2026` (chat completions), `responses_compat.py:747-750` (responses). Safe.

2. `DISCOVERY_TO_NIM` reverse map — `main.py:1078-1082`, used by `resolve_target_model` at `main.py:1102-1105`. Maps `claude-meta-llama-3.1-8b-instruct` → `meta/llama-3.1-8b-instruct`. This is **name normalization for the discovery alias form**, not fallback — the discovery alias is the same model with a different spelling. Borderline but defensible.

3. `_strip_context_suffix` — `main.py:971-977` — explicitly returns the model unchanged ("must not mutate a concrete model identity"). Safe.

4. `is_model_unavailable` / `_retired_models` — `main.py:1127-1141, 215-217` — returns `404 not_found_error` for retired models, **does not substitute**. Safe.

5. `apply_default_reasoning` — `main.py:885-910` — gated by `WRAPPER_AUTO_REASONING=1`, only injects `reasoning_effort` / `chat_template_kwargs`. Never touches `body['model']`. Safe.

6. `MODEL_REGISTRY.call_plan(...)` + `same_provider_model_id` check — `main.py:2462-2466`. Returns `500 MODEL_ID_MUTATION` if call-plan resolution changed the model id. **Active guard** against substitution. Safe.

### B. Dynamic alias system audit
- **Set how:** `load_alias_config` (`main.py:1058-1083`) reads `DYNAMIC_ALIAS_TARGET` env var at startup and calls `set_dynamic_alias_target(seed, force=True)`. `set_dynamic_alias_target` (`main.py:1036-1055`) refuses to bind from concrete client requests (`if not force and mid not in _known_models: return`). The comment at `main.py:999` ("last concrete model id seen from any client request") is **STALE** — concrete requests do not mutate alias state.
- **When client sends `model: "sonnet"`:** `resolve_target_model` (`main.py:1090-1119`) returns the DYNAMIC_ALIAS_TARGET value (e.g. `minimaxai/minimax-m3`). The body's `model` field is rewritten at `main.py:2048, 2061, 2129, 2145, 2319, 2809` BEFORE forwarding. Upstream NIM receives `minimaxai/minimax-m3`.
- **Client told?** NO. Response `model` field uses the resolved id, not the alias: `anthropic_compat.py:557` (`'model': model`) where `model` is the resolved id; `responses_compat.py:329` (`base_response(... 'model': model ...)`) and `:396` (`'model': data.get('model') or model`) — both use the resolved id or upstream's echo. Client sees `model: "minimaxai/minimax-m3"` in the response, never `"sonnet"`.
- **Violates "model A → forward model A"?** Strictly YES. Client requested `sonnet` (model A); upstream received `minimaxai/minimax-m3` (model B). However, this is **operator-configured name resolution** for a virtual Claude Code name that has no concrete existence on NVIDIA NIM. It is not "fallback on failure" — it is "operator told the wrapper that 'sonnet' means model X". If `DYNAMIC_ALIAS_TARGET` is unset, the alias passes through unchanged (`main.py:1115-1116`) and NIM will 404. **This is the intentional alias contract documented in `WRAPPER_CONTRACT.md:243`.**

### C. Key rotation balance — pseudocode
```python
# KeyPool._acquire_slot (key_pool.py:441-606) + _pick_key (628-649)
avail = [k for k in keys
         if not k.is_hard_blocked()
         and not k.is_model_blocked(model)             # ✓ model-aware filter
         and rpm_ok(k) and admit_ok(k)]
# also drop keys whose (key,model) RPM is ≥ 90% of learned (key,model) limit
ready = [k for k in avail if not near_key_model_limit(k, model)]
if ready:
    load = {k.label: k.effective_load for k in ready}    # current_rpm + in_flight
    min_load = min(load.values())
    candidates = [k for k in ready if load[k.label] == min_load]
    # round-robin among ties by _rr_index distance
    candidates.sort(key=lambda s: (idx(s)-_rr_index+len)%len)
    chosen = candidates[0]
    _rr_index = (idx(chosen)+1) % len(keys)
    return chosen
```
**Verdict:** least-loaded + RR tiebreak + hard-blocked + model-blocked filters + per-(key,model) RPM tracking. **Balanced.** Cannot get sticky unless every other key is hard-blocked AND model-blocked AND RPM-saturated.

### D. Model-scoped block audit
`KeyEntry.on_rate_limit` (`key_pool.py:149-171`):
```python
if scope == 'model' and model:
    self.model_blocks[model] = now + min(raw_secs, 10)    # ✓ key+model only
else:
    self.hard_blocked_until = now + min(raw_secs, 30)     # whole key
```
Scope decided by `_classify_429` (`key_pool.py:371-416`) using: (1) KEY_429_HINTS / MODEL_429_HINTS body strings; (2) corroboration across recent 429s (multi-model-on-key → key scope; multi-key-for-model → model scope); (3) RPM-ratio fallback (`rpm ≥ 0.8·hard` → key scope; else model scope). **Per-model block works correctly.** ✓
- 429 model-scoped → `block_model` ✓
- 429 key-scoped → whole-key block ✓
- 5xx: `mark_failure` is **NOT called** for 5xx — only `register_rate_limit` for 429 (`main.py:2551, 2609, 2857`). 5xx retries via `_classify_retry` (which returns True for `transient_failure`) but the key is not cooled down. Minor: a persistently-5xx key keeps getting picked.

### E. Retry loop audit — pseudocode
```python
# Server.proxy_openai (main.py:2450-2654) and _handle_catch_all (2820-2922)
model_id = body['model']                              # set ONCE
cand_model = model_id
body['model'] = cand_model                            # pinned, never mutated
attempt = 0
max_attempts = max(MAX_RETRIES+1, pool.total_keys)
while attempt < max_attempts:
    key = pool.acquire(model_id)                       # rotates keys, same model
    if not key: return 429 retry_later
    resp = POST(target_url, body, key)
    if resp == 429:
        pool.register_rate_limit(key, model_id, ra, body)   # model- or key-scope
        attempt += 1; continue
    if resp >= 400:
        classification = classify_upstream_error(resp.status, body)
        retryable = _classify_retry(status, classification)
        # _classify_retry returns True if retry_same_model=True OR state in
        # {key_rate_limited, model_rate_limited, transient_failure,
        #  account_forbidden, network_timeout}            ← model_rate_limited INCLUDED
        if retryable and attempt < max_attempts-1:
            attempt += 1; continue
        return resp                                    # non-retriable (400/404/422 etc.)
    return resp
return 429                                             # all keys exhausted
```
- Same model id on every retry ✓
- Never changes model ✓
- Stops when all keys exhausted ✓
- Distinguishes 400/404/422 (client error, no retry) from 429/5xx (retry) ✓
- **Correctly retries `model_rate_limited` across keys** — `_classify_retry` explicitly includes `model_rate_limited` in the retryable state list (`main.py:2426-2430`). The `BUG-RETRY1` comment notes this was a previous bug. ✓

### F. Response model field
- `/v1/messages` (Anthropic): `openai_to_anthropic(data, model_id, ...)` (`main.py:2393`); `anthropic_compat.py:557` sets `'model': model` = resolved id. **Client sees resolved id, not alias.** ⚠️ (acceptable for alias contract; not a fallback)
- `/v1/responses`: `responses_compat.py:329, 396` — `'model': model` or `data.get('model') or model`. Uses upstream's echo if present. ⚠️
- `/v1/chat/completions` & catch-all: returns upstream JSON verbatim (`main.py:2906`, `2654`). Model field = upstream's response. ⚠️
- No alias echoed back. **Response model field never matches the alias the client sent** (when alias resolution occurred).

---

## 2. nous (`src/main.py` — no separate key_pool.py)

### A. Model substitution / fallback violations
**No silent model substitution found.**

1. `resolve_model` (`main.py:590-604`) — concrete ids pass through unchanged; virtual aliases resolve to DYNAMIC_ALIAS_TARGET or pass through. No substitution.
2. Stale comment at `main.py:547` claims "When the client calls a concrete model … all aliases bind dynamically to that concrete id". **The actual code does NOT do this** — `set_dynamic_alias_target` is only invoked from `lifespan` (`main.py:1907-1910`) reading the env var. The comment is misleading and should be removed.
3. No DEFAULT_MODEL / FALLBACK_MODEL / REASONING_MODEL constants (explicitly removed per `main.py:470` comment).
4. `MODEL_REGISTRY.call_plan(...)` + `same_provider_model_id('nous', ...)` guard at `main.py:771-775`. Returns 500 `MODEL_ID_MUTATION` if call-plan mutated the id. ✓

### B. Dynamic alias system audit
- **Set how:** `lifespan` reads `DYNAMIC_ALIAS_TARGET` env var (`main.py:1907-1910`) and calls `set_dynamic_alias_target(seed, force=True)`. No request-path binding.
- **When client sends `model: "sonnet"`:** `resolve_model(body.get("model"))` is called inside `responses_to_chat` (`main.py:936`), `anthropic_to_openai` (`main.py:1113`), and route handlers. Returns the bound target if set, else the alias unchanged.
- **Client told?** NO. `openai_to_anthropic(model, ...)` (`main.py:1178-1234`) sets `"model": model` (line 1226) = resolved id. `respond_non_streaming(data, model)` (`main.py:1104-1108`) sets `"model": model` (line 1106) = resolved id. Responses JSON `/v1/chat/completions` returns upstream data verbatim.
- **Violates "model A → forward model A"?** Same answer as nvidia-python: strictly YES (operator-configured alias resolution), but it is name resolution, not failure fallback.

### C. Key rotation balance — pseudocode
```python
# KeyPool.acquire (main.py:236-257)
candidates = [k for k in keys
              if not k.is_blocked()
              and k.current_rpm() < self.hard_limit
              and (not exclude or k.label not in exclude)
              and not (model_id and k.is_model_blocked(model_id))]   # ✓ model-aware
min_load = min(k.effective_load for k in candidates)                 # rpm + in_flight
best = [k for k in candidates if k.effective_load == min_load]
entry = best[self._rr % len(best)]                                   # ✓ RR tiebreak
self._rr += 1
```
**Balanced.** Model-aware filtering, RR tiebreak, hard-limit enforcement. ✓

### D. Model-scoped block audit
`KeyEntry.block_model` (`main.py:171-178`) and `is_model_blocked` (`main.py:180-189`) — implemented. `KeyPool.mark_failure` (`main.py:265-286`):
```python
if model_scoped and model_id:
    block_model(model_id, ...)            # ✓ per-model only
    return
if status == 429:
    block(retry_after or 65, 'rate_limit')   # whole key
elif status in (401,402,403):
    block(..., 'auth_or_quota')
elif status >= 500 or status in (408,409):
    if model_id and status >= 500:
        block_model(model_id, ..., 'model_transient')   # ✓ per-model for 5xx
    else:
        block(..., 'transient')
```
- 429 model-capacity (`_looks_model_capacity_error` matches "no deployments available", "model unavailable", etc.): `_should_cooldown_key` returns False (`main.py:751-752`) → `mark_failure` NOT called. **AND** `_is_retriable_upstream_status` returns `bool(classify_upstream_error(...).retry_same_model)` = False (because `MODEL_RATE_LIMITED` has `retry_same_model=False` per `common/model/errors.py:114`). **Result: model-capacity 429 returns the error to the client WITHOUT trying another key.** ❌ **VIOLATION** of "sampai semua key menunjukkan error yang sama baru kemudian error diteruskan ke client".
- 429 plain (key-level): whole-key block, retry on next key ✓
- 5xx with model_id: per-model block ✓ (but only if `_is_retriable_upstream_status` returns True first, which it does for 5xx via `transient_failure`/`retry_same_model=True`)

### E. Retry loop audit — pseudocode
```python
# call_nous_with_retries (main.py:799-844)
attempts = max(1, KEY_POOL.total_keys)
tried_labels: Set[str] = set()
for attempt_i in range(attempts):
    entry = KEY_POOL.acquire(model_id=model_id, exclude=tried_labels)   # rotates, same model
    if not entry: break
    tried_labels.add(entry.label)
    status, result = post_nous(payload, entry.api_key, stream, ...)
    if status == 200: return ...
    if _is_retriable_upstream_status(status, result):
        if _should_cooldown_key(status, result):
            KEY_POOL.mark_failure(entry, status, ..., model_id=model_id)
        elif _looks_model_capacity_error(result) and model_id:
            KEY_POOL.mark_failure(entry, status, ..., model_id=model_id, model_scoped=True)
        continue
    return status, result, None                # non-retriable
return last_status, last_result, None
```
- Same model id on every retry ✓
- Never changes model ✓
- Stops when all keys exhausted ✓
- `_is_retriable_upstream_status = bool(classify_upstream_error(status, data).retry_same_model)`. For MODEL_RATE_LIMITED → `retry_same_model=False` → **NOT retriable** → returns immediately. ❌
- 400/404 (plain) → UNKNOWN state with `retry_same_model=False` → not retriable → returns immediately ✓ (mostly correct)
- **Does NOT correctly retry model-rate-limits across keys.** This is the same bug as opencode/blackbox. ❌

### F. Response model field
- `/v1/messages`: `openai_to_anthropic(model, ...)` → `"model": model` (resolved id). ⚠️
- `/v1/responses`: `respond_non_streaming(data, model)` → `"model": model` (resolved id). ⚠️
- `/v1/chat/completions`: returns upstream JSON verbatim. ⚠️

---

## 3. opencode (`src/main.py`, `src/key_pool.py`)

### A. Model substitution / fallback violations
**No silent model substitution found.**

1. `_normalize_model` (`main.py:426-446`) — concrete ids pass through; virtual aliases resolve to DYNAMIC_ALIAS_TARGET. No substitution.
2. Stale comment at `main.py:256` claims "Calling minimaxai/minimax-m3 or z-ai/glm-5.2 binds sonnet/haiku/opus/claude-* to that id." **The actual code does NOT do this** — only env-var seeding via `lifespan` (`main.py:1155-1158`). Comment is misleading.
3. `MODEL_REGISTRY.call_plan(...)` + `same_provider_model_id('opencode', ...)` guard at `main.py:601-607`. ✓

### B. Dynamic alias system audit
- **Set how:** `lifespan` reads `DYNAMIC_ALIAS_TARGET` env var (`main.py:1155-1158`). No request-path binding.
- **When client sends `model: "sonnet"`:** `_normalize_model` (`main.py:426-446`) returns the bound target if set, else the alias unchanged. Body's `model` field is rewritten at `main.py:1471, 1534, 1756, 1758` before forwarding.
- **Client told?** NO. `openai_to_anthropic(model, data)` (`main.py:790-834`) sets `"model": model` (line 832) = resolved id. `JSONResponse(data)` at `main.py:1781` returns upstream data verbatim. ⚠️
- **Violates "model A → forward model A"?** Same YES (operator-configured alias resolution).

### C. Key rotation balance — pseudocode
```python
# KeyPool.acquire (key_pool.py:148-173)
candidates = [k for k in keys
              if not k.is_hard_blocked()
              and k.current_rpm() < (k.hard_rpm or self.hard_limit)]
# ⚠️ MODEL PARAMETER IGNORED — no is_model_blocked check (method does not exist)
min_load = min(k.effective_load for k in candidates)
best = [k for k in candidates if k.effective_load == min_load]
key = best[self._rr % len(best)]
self._rr += 1
```
**Balanced for keys, but BLIND TO MODEL.** Cannot skip a key that is failing for this specific model. ⚠️

### D. Model-scoped block audit
**❌ NO model-scoped blocking exists.**
- `KeyEntry` has no `block_model` method, no `model_blocked_until` dict, no `is_model_blocked` method (`key_pool.py:21-103`).
- `KeyPool.mark_failure` (`key_pool.py:181-198`) **always** calls `key.block(seconds, reason)` which sets `hard_blocked_until` — **whole-key block**:
```python
if status_code == 429:
    key.block(retry_after or 65, 'rate_limit')            # ❌ WHOLE KEY
elif status_code in (401,402,403):
    key.block(retry_after or 300, 'auth_or_quota')
elif status_code >= 500 or status_code in (408,409):
    key.block(retry_after or 15, 'transient')             # ❌ WHOLE KEY
```
- **VIOLATION of "Error model A di key1 maka retry model A ke key lainnya. Dan model B masih boleh pakai key 1 karena berbeda."** When model A 429s on key1, the wrapper blocks key1 for ALL models — model B can no longer use key1. This is the **exact fatal concept error** the user described.

### E. Retry loop audit — pseudocode
```python
# proxy_request_with_pool (main.py:574-635)
attempts = max(1, pool.total_keys)
model_id = json_body.get('model', '')                    # set ONCE, never mutated
for _ in range(attempts):
    key_result = await pool.acquire()                     # ⚠️ no model_id passed!
    if not key_result: break
    status, data = await proxy_request(...)
    if status == 200: return ...
    classification = classify_upstream_error(status, data)
    if _is_retriable_upstream_status(status, data) and classification['retry_same_model']:
        if _should_cooldown_key(status, data):
            pool.mark_failure(key, status, retry_after, 'upstream', available_keys=avail)
            # ⚠️ no model_id passed — whole-key block
        pool.release(key); continue
    pool.release(key); return status, data, None
return last_status, last_data, None
```
- Same model id on every retry ✓
- Never changes model ✓
- Stops when all keys exhausted ✓
- `_is_retriable_upstream_status(status, data)` = `status in (401,402,403,408,409,429) or status >= 500`. **Plus** `classification['retry_same_model']` must be True. For MODEL_RATE_LIMITED (`retry_same_model=False`) → **NOT retriable** → returns immediately. ❌ Same bug as Nous.
- 400/404/422 → not retriable → returns immediately ✓
- **Does NOT retry model-rate-limits across keys.** ❌

### F. Response model field
- `/v1/messages` (translated): `openai_to_anthropic(model, data)` → `"model": model` (resolved id) at `main.py:832`. ⚠️
- `/v1/messages` (messages-native): `JSONResponse(data)` at `main.py:1781` returns upstream data verbatim. ⚠️
- `/v1/chat/completions`: `JSONResponse(_ensure_chat_message(data))` at `main.py:1507` returns upstream data verbatim. ⚠️

---

## 4. blackbox (`src/main.py`, `src/key_pool.py`)

### A. Model substitution / fallback violations
**No silent model substitution found.** Identical pattern to opencode.

1. `_normalize_model` (`main.py:341-350`) — concrete ids pass through; virtual aliases resolve to DYNAMIC_ALIAS_TARGET. No substitution.
2. `CURATED_FREE_MODELS` (`main.py:255-259`) is a discovery manifest, not a substitution map. Comment at `main.py:254` confirms: "Curated discovery manifest only; it never substitutes an inference model."
3. `MODEL_REGISTRY.call_plan(...)` + `same_provider_model_id('blackbox', ...)` guard at `main.py:559-563`. ✓

### B. Dynamic alias system audit
- **Set how:** `lifespan` reads `DYNAMIC_ALIAS_TARGET` env var (`main.py:1010-1013`). No request-path binding.
- **When client sends `model: "sonnet"`:** `_normalize_model` returns the bound target. Body rewritten at `main.py:1305, 1342, 1554` before forwarding.
- **Client told?** NO. Response returns upstream data verbatim (`main.py:1324` `JSONResponse(_ensure_chat_message(data))`). ⚠️
- **Violates "model A → forward model A"?** Same YES (operator-configured alias resolution).

### C. Key rotation balance — pseudocode
```python
# KeyPool.acquire (key_pool.py:129-147)
candidates = [k for k in keys
              if not k.is_blocked()
              and k.current_rpm() < (k.hard_rpm or self.hard_limit)]
# ⚠️ MODEL PARAMETER IGNORED (same as opencode)
min_load = min(k.effective_load for k in candidates)
best = [k for k in candidates if k.effective_load == min_load]
key = best[self._rr % len(best)]
self._rr += 1
```
**Balanced for keys, BLIND TO MODEL.** ⚠️

### D. Model-scoped block audit
**❌ NO model-scoped blocking exists.** Identical to opencode.
- `KeyEntry` (`key_pool.py:15-85`) has no `block_model`, no `model_blocked_until`, no `is_model_blocked`.
- `KeyPool.mark_failure` (`key_pool.py:155-165`) always whole-key blocks:
```python
if status_code == 429:
    key.block(retry_after or 65, 'rate_limit')            # ❌ WHOLE KEY
elif status_code in (401,402,403):
    key.block(retry_after or 300, 'auth_or_quota')
elif status_code >= 500 or status_code in (408,409):
    key.block(retry_after or 15, 'transient')             # ❌ WHOLE KEY
```
- **Same FATAL VIOLATION as opencode.** Model A 429 on key1 → key1 blocked for ALL models.

### E. Retry loop audit — pseudocode
```python
# proxy_request_with_pool (main.py:546-591)
attempts = max(1, pool.total_keys)
model_id = json_body.get('model', '')                     # set ONCE, never mutated
for _ in range(attempts):
    key_result = await pool.acquire(model_id)             # accepts model but acquire() ignores it
    if not key_result: break
    status, data = await proxy_request(...)
    if status == 200: return ...
    classification = classify_upstream_error(status, data)
    if _is_retriable_upstream_status(status) and classification['retry_same_model']:
        if _should_cooldown_key(status, data):
            pool.mark_failure(key, status, retry_after, 'upstream')
            # ⚠️ no model_id passed — whole-key block
        pool.release(key); continue
    pool.release(key); return status, data, None
return last_status, last_data, None
```
- Same model id on every retry ✓
- Never changes model ✓
- Stops when all keys exhausted ✓
- `_is_retriable_upstream_status(status)` (`main.py:501-504`) — let me re-check: actually blackbox uses the shared `is_retriable_status` from `common/translations/shared.py:261-263`: `status == 429 or status >= 500 or status in (408, 409)`. **PLUS** `classification['retry_same_model']` must be True. For MODEL_RATE_LIMITED (`retry_same_model=False`) → **NOT retriable** → returns immediately. ❌
- **Same model-rate-limit retry bug as opencode/nous.** ❌

### F. Response model field
- `/v1/chat/completions`: `JSONResponse(_ensure_chat_message(data))` at `main.py:1324` returns upstream data verbatim. ⚠️
- `/v1/messages`: returns upstream data verbatim or `openai_to_anthropic`-translated with resolved model id. ⚠️

---

## 5. openrouter (`src/main.py`, `src/key_pool.py`)

### A. Model substitution / fallback violations
**✅ NONE. Cleanest wrapper in the repo.**
- No `_normalize_model`, no `resolve_model`, no `DYNAMIC_ALIAS_TARGET`, no `_ALIAS_NAME_SET`, no `is_alias_name`, no `set_dynamic_alias_target` (grep confirms zero hits in `openrouter/src/main.py`).
- No `DEPRECATED_MODEL_REDIRECTS`.
- Model id is read from `body.get("model")` and forwarded as-is to upstream.
- `MODEL_REGISTRY.call_plan` is NOT invoked (no call_plan validation in `_proxy_request`). Model id passes through verbatim.

### B. Dynamic alias system audit
**N/A — openrouter has no alias system.** Clients must send concrete OpenRouter model ids (e.g. `openai/gpt-4o`, `anthropic/claude-3.5-sonnet`, `meta-llama/llama-3.1-8b-instruct:free`). No virtual `sonnet`/`haiku`/`opus` names. This is the **most conservative** design — zero risk of model substitution.

### C. Key rotation balance — pseudocode
```python
# KeyPool.acquire (key_pool.py:185-212)
candidates = [k for k in keys
              if not k.is_hard_blocked()
              and not (model and k.is_model_blocked(model))      # ✓ model-aware
              and k.current_rpm() < (k.hard_rpm or self.hard_limit)]
min_load = min(k.effective_load for k in candidates)             # rpm + in_flight
best = [k for k in candidates if k.effective_load == min_load]
key = best[self._rr % len(best)]                                 # ✓ RR tiebreak
self._rr += 1
```
**Balanced AND model-aware.** ✓

### D. Model-scoped block audit
**✅ Correct implementation.** `KeyEntry.block_model` (`key_pool.py:80-89`) and `is_model_blocked` (`key_pool.py:100-110`) — implemented. `KeyPool.mark_failure` (`key_pool.py:221-254`):
```python
if status_code == 429:
    cooldown = retry_after or 65
    if model:
        key.block_model(model, cooldown, 'rate_limit')     # ✓ per-model
    else:
        key.block(cooldown, 'rate_limit')
elif status_code in (401,402,403):
    key.block(retry_after or 300, 'auth_or_quota')          # whole key (correct for auth)
elif status_code >= 500 or status_code in (408,409):
    cooldown = retry_after or 15
    if model:
        key.block_model(model, cooldown, 'transient')       # ✓ per-model for 5xx
    else:
        key.block(cooldown, 'transient')
```
**Perfectly matches user's principle:** "Error model A di key1 maka retry model A ke key lainnya. Dan model B masih boleh pakai key 1 karena berbeda." ✓

### E. Retry loop audit — pseudocode
```python
# _proxy_request (main.py:580-728)
model_id = body.get("model", "")                            # set ONCE, never mutated
attempts = max(1, pool.total_keys)
for _ in range(attempts):
    acq = await pool.acquire(model=model_id)                # ✓ model-aware acquire
    if not acq: break
    resp = agent.request(method, url, json=body, ...)
    if resp.status >= 400:
        retry_after = _parse_retry_after(...) if 429 else None
        pool.mark_failure(key_obj, status_code=resp.status,
                          available_keys=..., model=model_id, retry_after=retry_after)
                          # ✓ model_id passed → model-scoped block
        if _is_retriable_status(resp.status):               # 429/5xx/408/409
            continue                                        # retry next key, same model
        return JSONResponse(...)
    return JSONResponse(...)
return 429 (all keys exhausted)
```
- Same model id on every retry ✓
- Never changes model ✓
- Stops when all keys exhausted ✓
- `_is_retriable_status(status)` (`common/translations/shared.py:261-263`) = `status == 429 or status >= 500 or status in (408, 409)`. **Does NOT consult `classify_upstream_error().retry_same_model`** — pure status-code check. This means model-capacity 429s ARE retried across keys (because 429 is retriable by status alone). ✓ **Correct per user's principle.**
- 400/404/422 → not retriable → returns immediately ✓
- **Best retry behavior of all 5 wrappers.** ✓

### F. Response model field
- All endpoints: returns upstream JSON verbatim (`main.py:644, 705, 713`). Model field = upstream's response. ✓ (No alias substitution, so the field matches the client's request.)

---

## CROSS-WRAPPER SUMMARY TABLE

| Aspect | nvidia-python | nous | opencode | blackbox | openrouter |
|---|---|---|---|---|---|
| **A. Silent model substitution** | None (DEPRECATED_MODEL_REDIRECTS only emits 410 error; alias resolution is operator-config) | None | None | None | **None — no alias system at all** |
| **B. Dynamic alias system** | Yes; env-seeded only; alias rewritten before forward; response echoes resolved id ⚠️ | Yes; env-seeded only; alias rewritten before forward; response echoes resolved id ⚠️ | Yes; env-seeded only; alias rewritten before forward; response echoes resolved id ⚠️ | Yes; env-seeded only; alias rewritten before forward; response echoes resolved id ⚠️ | **None — N/A** |
| **C. Key rotation: least-load + RR** | ✅ + model-aware filter + per-(key,model) RPM | ✅ + model-aware filter | ✅ but **MODEL PARAM IGNORED** ❌ | ✅ but **MODEL PARAM IGNORED** ❌ | ✅ + model-aware filter |
| **D. Model-scoped block on 429** | ✅ `block_model` for model-scope; `_classify_429` decides via hints + corroboration | ✅ `block_model` when `model_scoped=True`; ⚠️ but model-capacity 429 → no block + no retry ❌ | **❌ NONE — always whole-key block** | **❌ NONE — always whole-key block** | ✅ `block_model` if model provided (preferred) |
| **D. Model-scoped block on 5xx** | ⚠️ `mark_failure` NOT called for 5xx (no block, just retry) | ✅ `block_model` for 5xx with model_id | **❌ whole-key block** | **❌ whole-key block** | ✅ `block_model` for 5xx with model_id |
| **E. Retry loop: same model every retry** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **E. Retry loop: model_rate_limited retried across keys** | ✅ `_classify_retry` explicitly lists `model_rate_limited` | **❌ NOT retried** (retry_same_model=False) | **❌ NOT retried** (same bug) | **❌ NOT retried** (same bug) | ✅ retried (pure status-code check) |
| **E. Retry loop: 400/404/422 non-retriable** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **F. Response `model` field** | Resolved id (not alias) ⚠️ | Resolved id (not alias) ⚠️ | Resolved id (not alias) ⚠️ | Upstream echo ⚠️ | Upstream echo (matches request) ✅ |

---

## CRITICAL FINDINGS (ranked by severity)

### 🚨 SEVERITY 1 — FATAL CONCEPT ERROR (model B blocked because of model A's failure)

**Affected:** `opencode/src/key_pool.py:155-198`, `blackbox/src/key_pool.py:155-165`

OpenCode and Blackbox `KeyPool.mark_failure` ALWAYS call `key.block(...)` (whole-key block) regardless of which model failed. There is no `block_model` method, no `model_blocked_until` dict, no `is_model_blocked` method. When model A 429s on key1, key1 is blocked for ALL models — including model B which never failed.

This is the **exact fatal concept error** the user described: "Tidak boleh client request dengan model A, tapi di fallback ke model B" — well, here it's even worse: model B is denied service on key1 because of model A's failure, forcing model B to retry on other keys unnecessarily.

**Fix required:** Mirror openrouter's `KeyPool` (`openrouter/src/key_pool.py:80-110, 221-254`) — add `block_model(model_id, seconds, reason)`, `is_model_blocked(model_id)`, and pass `model_id` through `mark_failure` so per-model blocks are used.

### 🚨 SEVERITY 2 — Model-rate-limit not retried across keys

**Affected:** `nous/src/main.py:741-742, 827-833`, `opencode/src/main.py:524-529, 622`, `blackbox/src/main.py:501-504, 582`

Nous, OpenCode, and Blackbox all use `_is_retriable_upstream_status = bool(classify_upstream_error(status, data).retry_same_model)` (Nous) or a status-code check ANDed with `classification['retry_same_model']` (OpenCode/Blackbox). For MODEL_RATE_LIMITED state (`common/model/errors.py:111-114`), `retry_same_model` defaults to False. Combined with the AND, this means **a 429 with "model" / "deployment" / "capacity" in the body is NOT retried across keys** — the error is returned to the client immediately.

This directly violates: "Error model A di key1 maka retry model A ke key lainnya. Dan model B masih boleh pakai key 1 karena berbeda" and "sampai semua key menunjukkan error yang sama baru kemudian error diteruskan ke client".

**nvidia-python** is correct here: `_classify_retry` (`main.py:2426-2430`) explicitly includes `model_rate_limited` in the retryable state list, with a `BUG-RETRY1` comment noting this was a previous bug.

**openrouter** is also correct: `_is_retriable_status` is a pure status-code check (429 → retriable), so model-capacity 429s DO get retried across keys.

**Fix required:** In `nous/opencode/blackbox`, change `_is_retriable_upstream_status` to return True for any 429 (regardless of body content), or add `model_rate_limited` to the explicit retryable-state list like nvidia-python does.

### ⚠️ SEVERITY 3 — Stale comments suggesting request-path alias binding

**Affected:** `nvidia-python/src/main.py:999`, `nous/src/main.py:547`, `opencode/src/main.py:256`

These comments claim that concrete client requests mutate `_dynamic_alias_target` (binding all aliases to whatever concrete model the client just used). The actual code does NOT do this — `set_dynamic_alias_target` is only called from `lifespan` with the env-seed value, and the per-request `resolve_model`/`_normalize_model`/`resolve_target_model` functions never call `set_dynamic_alias_target`. The comments are misleading and should be removed/updated. The current behavior is correct (env-seed only); the comments suggest a previous (worse) design where one client's request could change the alias mapping for all other clients.

### ℹ️ SEVERITY 4 — Response `model` field doesn't echo alias

**Affected:** All 4 alias-supporting wrappers (nvidia-python, nous, opencode, blackbox).

When the client sends `model: "sonnet"` and the alias is resolved to `minimaxai/minimax-m3`, the response's `model` field is set to `minimaxai/minimax-m3` (the resolved id), not `"sonnet"` (the alias the client requested). For Anthropic /v1/messages and OpenAI /v1/chat/completions, this means clients that compare request.model == response.model will see a mismatch. Not a fallback, but a transparency issue. If the alias contract is "Claude Code sends sonnet and doesn't care what comes back", this is fine. If clients DO check, it's a bug.

**Fix (if needed):** In `openai_to_anthropic` and `respond_non_streaming`, accept and use the original requested alias for the response `model` field instead of the resolved id.

### ℹ️ SEVERITY 5 — nvidia-python 5xx doesn't cool down the key

**Affected:** `nvidia-python/src/main.py:2517-2654`.

For 5xx responses, `pool.register_rate_limit` is NOT called (only for 429). The retry loop retries via `_classify_retry` (which returns True for `transient_failure`), but the failing key is not cooled down. If a key persistently returns 5xx, it stays in rotation and may be picked again on the next request. Not a model-substitution issue, but a load-balancing imperfection.

**Fix (optional):** Add a `pool.mark_failure(key, model_id, status, ...)` call for 5xx responses, scoped to `block_model` (per-model only) so other models on the same key are unaffected.

### ℹ️ SEVERITY 6 — OpenCode/Blackbox `acquire()` ignores `model` parameter

**Affected:** `opencode/src/key_pool.py:148-173`, `blackbox/src/key_pool.py:129-147`.

`acquire(self, model: str = '')` accepts the model parameter but never uses it for filtering. Blackbox's `proxy_request_with_pool` passes `model_id` to `acquire(model_id)` (`main.py:565`) but it's silently dropped. OpenCode's `proxy_request_with_pool` doesn't even bother passing it (`main.py:586`: `await pool.acquire()`). This is a direct consequence of Severity 1 (no model-scoped state to filter on).

**Fix:** Resolved automatically once Severity 1 is fixed — add `is_model_blocked(model)` check in `acquire()`.

---

## NEXT ACTIONS

1. **IMMEDIATE — fix Severity 1 & 2 in opencode and blackbox.** These two wrappers have the same broken `KeyPool` that violates the user's most explicit principle. Copy `openrouter/src/key_pool.py` (which is correct) and adapt naming. Also fix the retry loop in both wrappers' `proxy_request_with_pool` to:
   - Pass `model_id` to `pool.acquire(model=model_id)`.
   - Pass `model_id` to `pool.mark_failure(..., model=model_id)`.
   - Make `_is_retriable_upstream_status` return True for any 429 (or include `model_rate_limited` in the retryable-state list).

2. **IMMEDIATE — fix Severity 2 in nous.** Same retry-loop fix as opencode/blackbox: make `_is_retriable_upstream_status` return True for 429 even when body contains model-capacity hints. The `_should_cooldown_key` + `block_model` path is already correct — only the retriable check is wrong.

3. **SOON — fix Severity 3 stale comments.** Remove or rewrite the misleading comments in nvidia-python (`main.py:999`), nous (`main.py:547`), opencode (`main.py:256`) that suggest request-path alias binding.

4. **OPTIONAL — fix Severity 4 response model field.** If the alias contract requires response.model == request.model, propagate the original alias through to `openai_to_anthropic` / `respond_non_streaming` and echo it back. If the contract is "Claude Code doesn't care", leave as-is.

5. **OPTIONAL — fix Severity 5 nvidia-python 5xx cooldown.** Add `mark_failure` call for 5xx with model-scoped block.

6. **REGRESSION TEST — add a test that asserts:** for every wrapper, when model A returns 429 on key1, (a) key1 is blocked for model A only, (b) key1 is still selectable for model B, (c) the retry loop tries key2 with the same model A. The existing `tests/test_transparent_model_contract.py` checks for absence of `select_fallback_model` symbol but doesn't exercise the runtime model-scoped retry path.

---

## RAW EVIDENCE INDEX (file:line)

### nvidia-python
- Alias set: `src/main.py:986-998` (`_ALIAS_NAME_SET`), `:999` (`_dynamic_alias_target` — STALE COMMENT)
- Alias resolve: `src/main.py:1090-1119` (`resolve_target_model`)
- Alias seeded: `src/main.py:1070-1073` (`load_alias_config`)
- Alias injected into body: `src/main.py:2048, 2061, 2129, 2145, 2319, 2809`
- Response model: `src/anthropic_compat.py:557`, `src/responses_compat.py:329, 396`
- KeyPool._pick_key: `src/key_pool.py:628-649` (least-load + RR)
- KeyPool._acquire_slot: `src/key_pool.py:441-606` (model-aware filter at :492)
- KeyEntry.on_rate_limit: `src/key_pool.py:149-171` (model vs key scope)
- _classify_429: `src/key_pool.py:371-416`
- proxy_openai retry loop: `src/main.py:2517-2654`
- _classify_retry: `src/main.py:2414-2430` (explicitly retries `model_rate_limited`)
- DEPRECATED_MODEL_REDIRECTS: `src/main.py:781-793` (informational only, returns 410)
- MODEL_ID_MUTATION guard: `src/main.py:2462-2466`

### nous
- Alias set: `src/main.py:548-555` (`_ALIAS_NAME_SET`), `:556` (`_dynamic_alias_target`)
- Alias resolve: `src/main.py:590-604` (`resolve_model`)
- Alias seeded: `src/main.py:1907-1910` (lifespan)
- Alias injected: `src/main.py:936, 1113` (responses_to_chat, anthropic_to_openai)
- Response model: `src/main.py:1106, 1226`
- Stale comment: `src/main.py:547`
- KeyPool.acquire: `src/main.py:236-257` (model-aware)
- KeyEntry.block_model: `src/main.py:171-178`
- KeyPool.mark_failure: `src/main.py:265-286`
- Retry loop: `src/main.py:799-844`
- _is_retriable_upstream_status: `src/main.py:741-742` (BUG: returns False for model_rate_limited)
- _looks_model_capacity_error: `src/main.py:745-747`
- MODEL_ID_MUTATION guard: `src/main.py:771-775`

### opencode
- Alias set: `src/main.py:257-264` (`_ALIAS_NAME_SET`), `:265` (`_dynamic_alias_target`)
- Alias resolve: `src/main.py:426-446` (`_normalize_model`)
- Alias seeded: `src/main.py:1155-1158` (lifespan)
- Alias injected: `src/main.py:1471, 1534, 1756-1758`
- Response model: `src/main.py:832` (`openai_to_anthropic`), `:1781` (verbatim)
- Stale comment: `src/main.py:256`
- KeyPool.acquire: `src/key_pool.py:148-173` (❌ MODEL PARAM IGNORED)
- KeyPool.mark_failure: `src/key_pool.py:181-198` (❌ NO block_model)
- Retry loop: `src/main.py:574-635`
- _is_retriable_upstream_status: `src/main.py:524-529` (BUG: ANDed with retry_same_model)
- MODEL_ID_MUTATION guard: `src/main.py:601-607`

### blackbox
- Alias set: `src/main.py:261-268` (`_ALIAS_NAME_SET`), `:269` (`_dynamic_alias_target`)
- Alias resolve: `src/main.py:341-350` (`_normalize_model`)
- Alias seeded: `src/main.py:1010-1013` (lifespan)
- Alias injected: `src/main.py:1305, 1342, 1554`
- Response model: `src/main.py:1324` (verbatim)
- KeyPool.acquire: `src/key_pool.py:129-147` (❌ MODEL PARAM IGNORED)
- KeyPool.mark_failure: `src/key_pool.py:155-165` (❌ NO block_model)
- Retry loop: `src/main.py:546-591`
- _is_retriable_upstream_status: `src/main.py:501-504` (BUG: ANDed with retry_same_model)
- MODEL_ID_MUTATION guard: `src/main.py:559-563`

### openrouter
- No alias system (confirmed by grep — zero hits for `_normalize_model`, `DYNAMIC_ALIAS_TARGET`, `_ALIAS_NAME_SET`, `is_alias_name`, `set_dynamic_alias_target` in `src/main.py`)
- KeyPool.acquire: `src/key_pool.py:185-212` (✅ model-aware)
- KeyEntry.block_model: `src/key_pool.py:80-89`
- KeyEntry.is_model_blocked: `src/key_pool.py:100-110`
- KeyPool.mark_failure: `src/key_pool.py:221-254` (✅ model-scoped preferred)
- Retry loop: `src/main.py:580-728`
- _is_retriable_status: `common/translations/shared.py:261-263` (✅ pure status-code check)

### common (shared infrastructure)
- `common/model/call_plan.py:18-21` — raises CallPlanError if `model_substitution` or `provider_substitution` is True in policy. ✅ Hard guard.
- `common/model/contracts.py:43-45` — `ModelRef.model_changed` always returns False; comment: "Alias resolution is not model substitution." (Architecture's stance on alias resolution.)
- `common/model/contracts.py:120-125` — default policy: `model_substitution=False, provider_substitution=False, key_rotation=True`.
- `common/model/registry.py:104-105` — register_profile refuses profiles with substitution policy.
- `common/model/errors.py:68-138` — `classify_upstream_error`. ⚠️ Note line 111-114: 429 with "model"/"deployment"/"capacity" in body → `MODEL_RATE_LIMITED` with `retry_same_model=False` (default). This is the ROOT CAUSE of Severity 2 — the shared classifier tells wrappers not to retry model-rate-limits, and 3 of 5 wrappers believe it.
- `common/translations/shared.py:261-263` — `is_retriable_status` (pure status-code check, used by openrouter).
