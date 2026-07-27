# Deep Code Audit — proxy-wrapper (opencode / blackbox / common / model-registry)

Audit date: 2026-07-27 · Mode: READ-ONLY (no files modified, no services touched) · Evidence: source at HEAD `21d0f79`, running processes, systemd units, git state.

---

## 1. opencode (`opencode/src/main.py`, `key_pool.py`, `metrics.py`)

| ID | Sev | Location | Description | Failure scenario | Suggested fix |
|----|-----|----------|-------------|------------------|---------------|
| OC-1 | **CRITICAL** | `key_pool.py:21-41` (`Mutex`) | Hand-rolled mutex is not cancellation-safe. If a task waiting in `acquire()` is cancelled (client disconnect while queued), its future stays in `_queue` as *cancelled*. `release()` then pops it, sees `fut.done()`, sets no result **and does not wake the next waiter or clear `_locked`**. | Under load (queue non-empty) one client disconnect while waiting for the pool lock permanently wedges the lock → every subsequent `pool.acquire()` awaits forever → wrapper accepts connections but all inference requests hang. | Replace `Mutex` with `asyncio.Lock`, or in `acquire()` add a `try/except CancelledError` that removes/compensates the future, and make `release()` loop until it wakes a non-done future or unlocks. |
| OC-2 | **HIGH** | `main.py:492-498` (`proxy_request_with_pool`) | Call-plan identity check runs **after** the upstream request. When `is_stream=True` and upstream returned 200, `data` is a live `aiohttp` response; the `MODEL_ID_MUTATION` / `MODEL_CALL_PLAN_INVALID` branches do `pool.release(key)` and return **without `resp.release()`**. | Any alias/manifest misconfiguration (e.g. corrupt `manifests/providers/opencode.json` → empty protocols → `CallPlanError`) leaks one upstream connection per streaming request until the connector pool (200) is exhausted → all requests stall. Also: upstream call already burned quota before the 400 is returned. | Perform the call-plan check *before* `proxy_request()`; on the error branches, `resp.release()` when `is_stream and status == 200`. |
| OC-3 | **HIGH** | `main.py:934-942` (`_auth_check`) + `main.py:1427-1442` (`/dashboard`) | `/dashboard` has **no auth** and injects the plaintext `BEARER_TOKEN` into the returned HTML (`<meta name="wrapper-bearer-token">`). | Any process/user able to reach 127.0.0.1:9103 (or any page loaded in a local browser, since CORS allows all localhost origins with credentials) reads the token via `GET /dashboard` and gains full authenticated proxy access. Defeats the purpose of BEARER_TOKEN entirely. | Require `_auth_check` on `/dashboard`, or stop embedding the token (have the dashboard prompt for it / use a session cookie). |
| OC-4 | **HIGH** | `main.py:800-828` (`stream_passthrough`), `main.py:1174-1284` (responses gen), `main.py:1363-1398` (anthropic gen) | Heartbeats only fire *after a chunk arrives* (`async for … : yield chunk; if elapsed: yield heartbeat`). This is exactly BUG-CODEX2 documented in `CROSS_WRAPPER_BUG_POLICY.md` and fixed in nous (`wrapper_nous.py:1042-1059`, `asyncio.wait_for`), not here. The two translate generators have **no heartbeat at all**. | Reasoning model silent for 30-120s → zero bytes to client → agent SDK / LB idle-timeout kills the stream mid-generation. | Port nous's `asyncio.wait_for(resp.content.iter_any().__anext__(), timeout=HEARTBEAT_MS/1000)` pattern into `stream_passthrough` and both translate generators. |
| OC-5 | **MED** | `main.py:810-820` | Heartbeat comment `b": heartbeat\n\n"` is yielded between `iter_any()` chunks, which are **not SSE-line aligned**. | If a chunk ends mid-`data:` line, the injected comment splits the SSE line → client parser drops or corrupts the event. Low probability per chunk, near-certain over long streams. | Buffer to line boundaries before allowing a heartbeat, or only heartbeat when the last yielded byte was `\n`. |
| OC-6 | **MED** | `main.py:68-73, 389-393` | Circuit breaker only ever calls `before_request()`; `record_failure()`/`record_success()` are never called (unlike nvidia, `nvidia-python/src/main.py:2157/2270`). `_failure_count` stays 0 forever. | Breaker can never open — dead code providing a false sense of upstream protection; during an upstream outage every request still pays full timeout. | Call `record_failure()` on 5xx/exception and `record_success()` on 200 in `proxy_request`. |
| OC-7 | **MED** | `main.py:79-89` + `main.py:412, 424` | If `common.translations` import fails, `_USING_SHARED_TRANSLATIONS = False` but **no fallbacks are defined**: `_normalize_upstream_error`, `_strip_cache`, `_parse_dsml_from_text`, `_repair_orphan_tool_messages`, `AnthropicStreamState` are undefined names. | A broken `PYTHONPATH`/partial deploy yields a wrapper that *starts fine* and then throws `NameError` on the first upstream error / anthropic request instead of failing fast at boot. | Fail hard at import (`raise`) instead of setting a flag. |
| OC-8 | **MED** | `main.py:269-282` (`check_rate_limit`) + `main.py:1310` | Per-IP limiter dict `_rate_limit_store` is keyed by the **client-controlled** first `X-Forwarded-For` value and is never globally pruned (only per-key on revisit). Applied **only** to `/v1/messages` — `/v1/chat/completions` and `/v1/responses` are unlimited. | (a) attacker rotates XFF values → unbounded dict growth (memory leak) and full limiter bypass; (b) inconsistent protection across endpoints. | Key by `request.client.host` (trust XFF only behind a known proxy), periodically sweep empty keys, apply to all POST endpoints. |
| OC-9 | **MED** | `main.py:976, 987, 1008` (`/v1/models`) | `MODEL_STORE.get_catalog()` / `status_map()` are synchronous SQLite calls executed directly on the event loop inside an async handler. | Under concurrent traffic + slow disk / WAL contention, all in-flight streams stall for the duration of the query. | `await asyncio.to_thread(...)` like the `record_*_async` paths already do. |
| OC-10 | **LOW** | `main.py:1256-1260` | On mid-stream upstream exception the responses generator fabricates `[upstream stream error: …]` as normal `output_text.delta` and then emits `response.completed` with `status: completed`. | Codex-style clients persist the error string as a successful assistant answer; retries never trigger. | Emit `response.failed` / an error event instead of a "completed" envelope. |
| OC-11 | **LOW** | `main.py:685-696, 1280-1299` (`_RESPONSE_STORE`) | Global in-memory conversation store (cap 200 entries) with unauthenticated key space (`previous_response_id` is client-supplied). Entries can each hold full multi-MB conversations. | Cross-tenant conversation disclosure if an ID is guessed/shared; bounded but potentially large RSS. | Namespace store keys by auth principal; cap per-entry size; consider TTL eviction. |
| OC-12 | **LOW** | `main.py:1093` | `family == "google"` routes **chat POST bodies** to `{BASE}/models/{model}` — a catalog URL, not a generation endpoint. | Every `gemini-*` chat request 404s/405s upstream and cools down a key via `mark_failure`. | Route gemini via the correct Zen surface, or fall back to `/chat/completions`. |
| OC-13 | **LOW** | `key_pool.py:196-207` (`mark_failure`) | Any single 5xx/408/409 blocks a key ≥15s; 401/402/403 for 300s. With small pools (1–2 keys) a transient upstream blip flips `/health` to `degraded` and sheds traffic. | One upstream 500 → sole key blocked → subsequent requests get "No capacity" 503 for 15s. | Require N consecutive failures before blocking, or shorten transient cooldown when `available_keys==0`. |
| OC-14 | **LOW** | `metrics.py:65-71, 85-86` | `errors` counter can never increment (no caller passes `status_code`); metrics recorded only on non-stream chat success; snapshot persisted only in graceful shutdown, lost on SIGKILL/OOM. | `/metrics` under-reports: error_rate always 0, streaming traffic invisible, counters reset on crash. | Record on all endpoints incl. error paths; persist periodically. |
| OC-15 | **INFO** | `main.py:1-2` | `import sys` appears **above** the shebang line; shebang is inert. | — | Move shebang to line 1. |
| OC-16 | **INFO** | `main.py:285`, `opencode/.env` | `ANTI_SILENCE` read but never used; `.env` has stale `REQUEST_TIMEOUT_MS=900` (code uses `REQUEST_TIMEOUT_SEC`) — ms/sec confusion trap. | Operator "tunes" a knob that does nothing. | Delete dead config. |
| OC-17 | **INFO** | `main.py:866-869` | Catalog refresh loop sleeps `MODEL_CATALOG_REFRESH_SEC` (86400s) *before* the first refresh — no boot-time refresh. | Fresh deploy serves fallback/stale catalog for up to a day. | `await refresh_model_catalog_once()` before entering the loop. |
| OC-18 | **INFO** | `main.py:830-844` | Watchdog observer thread never stopped on shutdown; `BEARER_TOKEN`, timeouts, `HEARTBEAT_MS` are module constants and do **not** hot-reload. | Token rotation via `.env` silently ineffective until restart. | Re-read `BEARER_TOKEN` in `_auth_check`; stop observer in lifespan teardown. |

---

## 2. blackbox (`blackbox/src/main.py`, `key_pool.py`, `metrics.py`)

| ID | Sev | Location | Description | Failure scenario | Suggested fix |
|----|-----|----------|-------------|------------------|---------------|
| BB-1 | **CRITICAL** | `key_pool.py:15-34` (`Mutex`) | Same cancellation deadlock as OC-1 (identical class). | Same: one cancelled waiter permanently wedges the key pool. | Same as OC-1. |
| BB-2 | **HIGH** | `main.py:404-409` | Same streaming-response leak on `MODEL_ID_MUTATION`/`MODEL_CALL_PLAN_INVALID` as OC-2 (no `resp.release()` for stream+200). | Connection-pool exhaustion under manifest misconfiguration. | Same as OC-2. |
| BB-3 | **HIGH** | `blackbox/.env` (`BEARER_TOKEN=` empty) + `main.py:761-769` | `BEARER_TOKEN` is **empty** in the live `.env` → `_auth_check` returns immediately → *every* endpoint unauthenticated. Only the systemd `--host 127.0.0.1` bind protects it. No warning logged when running open. | Any local process gets free proxied access to paid upstream keys; if ever started via `main()`/`.env` (see BB-10) exposed to the whole network. | Set a token; log a loud startup warning when auth is disabled (parity with opencode `main.py:925-928`). |
| BB-4 | **HIGH** | `main.py:1181-1196` (`/dashboard`) | Same unauthenticated bearer-token-in-HTML leak as OC-3 (currently moot because token is empty — but becomes live the moment BB-3 is fixed naively). | Same as OC-3. | Same as OC-3. |
| BB-5 | **HIGH** | `main.py:651-670`, `1117-1151`, `974-1058` | Same idle-heartbeat gap (BUG-CODEX2) as OC-4: passthrough heartbeats only on data arrival; anthropic & responses translate generators have no heartbeat. | Same client idle-timeout kills. | Same as OC-4. |
| BB-6 | **MED** | `main.py:660-661` | Same mid-chunk heartbeat SSE corruption as OC-5. | Same. | Same. |
| BB-7 | **MED** | `main.py:64-70, 321-325` | Same never-recording circuit breaker as OC-6. | Breaker never opens. | Same. |
| BB-8 | **MED** | `main.py:74-83, 1116` + `main.py:1203-1204` | `AnthropicStreamState` referenced in `anthropic_messages` but only bound at the *bottom of the module*. If the shared import fails, the wrapper still boots and every streaming `/v1/messages` raises `NameError`; `_normalize_upstream_error`/`_strip_cache`/`_repair_orphan_tool_messages` likewise undefined. | Broken deploy fails at request time, not boot. | Fail fast on import error; bind the alias next to the import. |
| BB-9 | **MED** | `main.py:836` (`_model_list_with_aliases`) → `model_state.py:321-383` | `MODEL_STORE.status_for(mid)` called **once per model**, each call runs `status_map()` — a *full-table* SQLite scan on a fresh connection — synchronously on the event loop. N full scans per `/v1/models` request (opencode does a single `status_map()` — drift). | `/v1/models` latency scales O(N × table size); event loop blocked. | Fetch `status_map()` once per request and index it; move to `to_thread`. |
| BB-10 | **MED** (F1 confirmed) | `blackbox/.env` (`LISTEN_PORT=9108`, `LISTEN_HOST=0.0.0.0`) vs systemd (`--host 127.0.0.1 --port 9104`) | Config drift confirmed: `.env` says public 0.0.0.0:9108; systemd overrides with loopback:9104. `.env` values are used by `main()`/`uvicorn.run` on direct launch. | Operator debugging with `python -m src.main` unknowingly exposes an **unauthenticated** (BB-3) proxy on 0.0.0.0:9108. | Align `.env` to `LISTEN_HOST=127.0.0.1`, `LISTEN_PORT=9104`. |
| BB-11 | **MED** | `main.py:118-132, 905, 1086` | Same spoofable/unbounded per-IP limiter as OC-8; applied to chat+messages but **not** `/v1/responses`. | Same memory growth + bypass. | Same as OC-8. |
| BB-12 | **LOW** | `main.py:937-966` (`/v1/responses`) | No outer try/except (opencode wraps everything → 502 JSON). `responses_to_chat` does bare `int(body['max_output_tokens'])` / `float(body[k])`; no max_tokens cap here (BUG-SEC3 check only in `_validate_chat_body`, which responses doesn't call). | `{"max_output_tokens": "abc"}` → unhandled `ValueError` → raw 500 with no API-shaped error body. | Wrap endpoint in try/except returning a shaped 400/502; reuse `_validate_chat_body`. |
| BB-13 | **LOW** | `main.py:856-859` (`/v1/capabilities`) | No `_auth_check` (opencode has it — drift). | Enumeration of account-scoped availability without auth. | Add `_auth_check(request)`. |
| BB-14 | **LOW** | `main.py:537-555` (`openai_to_anthropic`) | No DSML leak parsing (opencode calls `_parse_dsml_from_text`). | MiniMax tool markup leaks verbatim into client-visible text on blackbox. | Import and apply `parse_dsml_from_text` like opencode. |
| BB-15 | **LOW/INFO** | `metrics.py` (identical to opencode's) | Same dead `errors` counter / shutdown-only persistence as OC-14. | Same. | Same. |

---

## 3. common/

| ID | Sev | Location | Description | Failure scenario | Suggested fix |
|----|-----|----------|-------------|------------------|---------------|
| CM-1 | **HIGH** | `model/identity.py:75` (`AliasResolver.resolve`) | `next((self.bindings.get((kind, scope, alias)) for kind, scope in scopes), None)` returns the **first yielded value even when it is `None`** — only the first scope in the chain is ever consulted; bindings in later scopes never found. Wrappers bind aliases under `("wrapper", provider)` but call `resolve()` with an empty scope chain (only `("global","*")`) — registry-side alias bindings structurally unreachable. | Registry alias resolution silently no-ops. Currently masked because wrappers pre-resolve aliases in `_normalize_model`, but any consumer relying on `LocalModelRegistry.resolve` / central `/v1/resolve` gets wrong results. | `next((b for kind, scope in scopes if (b := self.bindings.get((kind, scope, alias))) is not None), None)`; wrappers pass `scope_chain=[("wrapper", provider)]`. |
| CM-2 | **MED** | `circuit_breaker.py:106-131` | State machine correct post-BUG-ECB1, but: (a) HALF_OPEN admits **unlimited concurrent probes** — after `recovery_timeout` a thundering herd hits a sick upstream; (b) `asyncio.Lock` created at import time — binds to first loop (uvicorn reload/multi-loop tests). Main defect is caller misuse (OC-6/BB-7). | Recovery moment sends full traffic burst upstream instead of one probe. | Gate HALF_OPEN to one in-flight probe. |
| CM-3 | **MED** | `middleware.py:36-65` (`RequestSizeLimiter`) | Limits by `Content-Length` header only; `Transfer-Encoding: chunked` bypasses the cap entirely. | Memory-exhaustion guard bypassable by any client that streams the body. | Wrap `receive` to count body bytes and abort past `max_bytes`. |
| CM-4 | **MED** | `model/central_client.py:54-68` (`_ensure_session`) | Racy double-checked init: second coroutine **closes the session the first just created (possibly with in-flight requests) and builds another**. Fast-path read outside the lock. | Sporadic `Session is closed` on observation posts; silently drops telemetry. | Re-check validity inside the lock, return early if healthy session exists. |
| CM-5 | **LOW** | `model_state.py:404-427` | `_maybe_prune()` only invoked from `record_error()`. `_last_status_write` grows via successful `record_status()` without bound until an error happens. | Slow memory/disk creep during long healthy periods. | Call `_maybe_prune()` from `record_status()` too. |
| CM-6 | **LOW** | `translations/anthropic_stream.py:86-179` | If upstream sends content chunks *after* `finish_reason`, `translate_chunk` opens new `content_block_start` events **after `message_stop`** — protocol violation. `force_done` reports zero usage even when a usage chunk was seen. | Strict Anthropic SDK parsers error on post-stop events; usage lost on abnormal termination. | Guard `translate_chunk` with `if self.finished: return events`; retain last-seen usage for `force_done`. |
| CM-7 | **LOW** | `logging_utils.py:36-79` | `logging.basicConfig(handlers=[...])` on root logger; FileHandler has no rotation; second `setup_logging` call is a silent no-op. | Unbounded `*.log` growth. | Use `RotatingFileHandler`; attach to named logger. |
| CM-8 | **INFO** | `model/sanitize.py`, `model/validation.py` | Solid: secrets regex-redacted, sensitive keys blanked, NaN/Inf handled, IDs bounded. | — | — |
| CM-9 | **INFO** | `model_state.py:60-68` | `_initialized` flag not thread-safe (`_connect` from `to_thread` workers); worst case duplicate idempotent `_init` — harmless. WAL + busy_timeout correct. | — | — |

---

## 4. model-registry (`service.py`, `manifests/`)

| ID | Sev | Location | Description | Failure scenario | Suggested fix |
|----|-----|----------|-------------|------------------|---------------|
| MR-1 | **HIGH** | `service.py:42-44` default DB path vs `/root/wrapper/registry-state.db` | State-location drift: service default is `model-registry/registry-state.db` which **does not exist**, while a 2.5 MB `registry-state.db` (4,723 events, last write 2026-07-24) sits orphaned at repo root. Running service has an empty knowledge base; no observation/catalog ingest has succeeded since Jul-25 restart (central publication silently failing or disabled — opencode `.env` lacks `MODEL_REGISTRY_URL` entirely). | The "central model intelligence" plane serves nothing and accumulates nothing; wrappers' `failed_posts/dropped_observations` climb invisibly. | Pin `MODEL_REGISTRY_DB` in systemd unit; migrate/inspect the root DB; alert on `stats()['failed_posts']`. |
| MR-2 | **MED** | `service.py:232-262` (`observation`), `48-69` (`CentralRegistry`) | `async def` handlers run sync SQLite directly on event loop; plain `def` handlers run in threadpool and mutate/read the same unlocked `central.registries` / `profiles` dicts. | (a) loop stalls during observation bursts; (b) cross-thread dict race → `RuntimeError: dictionary changed size during iteration` → 500s. | Uniform async + `to_thread` for SQLite; lock around `CentralRegistry`. |
| MR-3 | **LOW** | `service.py:86-96` | Auth good: `/internal/*` fails closed without admin token. Gaps: token compare non-constant-time; public read endpoints have no auth/rate-limit/size-limit — acceptable only on 127.0.0.1 (verified). | Timing side-channel theoretical. | `hmac.compare_digest`; add `RequestSizeLimiter`. |
| MR-4 | **LOW** | `service.py:265-269` (`/internal/status`) | Provider param bypasses `validate_provider_name`; garbage provider → unhandled 500. | Cosmetic 500s; inconsistent validation. | Route through `validate_provider_name`. |
| MR-5 | **INFO** | hot-path dependency check | **Good**: wrappers do NOT network-call the registry per request — `MODEL_REGISTRY.call_plan` is in-process; central publication is a bounded async queue with mini-circuit-breaker. No SPOF/latency in inference path. Residual risk: empty/corrupt `manifests/providers/{provider}.json` → `_default_protocols()` = `()` → `CallPlanError` → every request 400 (and OC-2/BB-2 leak). | Manifest corruption = total-outage lever, discovered per-request *after* the upstream call. | Validate manifests at startup; refuse to boot when protocols empty. |

---

## 5. Cross-wrapper drift table (bug fixed in X, missing in Y/Z)

| ID | Pattern / bug | Fixed / correct in | Missing / broken in | Evidence |
|----|---------------|--------------------|---------------------|----------|
| DR-1 | **Idle heartbeat (BUG-CODEX2)** | nous: `wrapper_nous.py:1042-1059` (`asyncio.wait_for`) | opencode `main.py:810-820`; blackbox `main.py:655-662`; both translate generators (no heartbeat); nvidia `main.py:2209-2226` (no heartbeat) | Flagged in `CROSS_WRAPPER_BUG_POLICY.md` Example 1 as "needs fix" — still unfixed. |
| DR-2 | **Circuit breaker outcome recording** | nvidia: `main.py:2157/2270` | opencode `main.py:391`; blackbox `main.py:323`; nous `wrapper_nous.py:558` | Breakers in 3 of 4 wrappers can never trip. |
| DR-3 | **Mutex cancellation deadlock** | none | opencode `key_pool.py:21-41`; blackbox `key_pool.py:15-34`; nvidia `src/key_pool.py` | Same buggy class copy-pasted 3×. |
| DR-4 | **Orphan tool-message repair (Anthropic→OpenAI)** | blackbox `main.py:517` | opencode `main.py:627` (repair only in responses path) | Orphan `tool_result` → upstream 400 on opencode only. |
| DR-5 | **Image content blocks in `anthropic_to_openai`** | blackbox `main.py:488-495` | opencode `main.py:591-607` (silently dropped) | Vision requests lose images on opencode. |
| DR-6 | **DSML tool-markup leak parsing** | opencode `main.py:652` | blackbox `main.py:537-555` | MiniMax DSML leaks to clients on blackbox. |
| DR-7 | **Per-IP rate limiting coverage** | — (all partial) | opencode: only `/v1/messages`; blackbox: chat+messages, not `/v1/responses` | Same helper, inconsistently wired. |
| DR-8 | **Endpoint exception containment** | opencode (try/except → shaped 502) | blackbox `/v1/responses` & `/v1/messages` non-stream unwrapped → raw 500 | e.g. `int(body['max_output_tokens'])` on bad input. |
| DR-9 | **max_tokens overflow cap (BUG-SEC3)** | both chat endpoints | neither `/v1/responses` path | Cap applied per-endpoint, not shared validation. |
| DR-10 | **Open-auth warning** | opencode logs warning | blackbox silent open when token unset (and its token IS unset — BB-3) | |
| DR-11 | **Auth on `/v1/capabilities`** | opencode `main.py:1027` | blackbox `main.py:856-859` | |
| DR-12 | **Model-status lookup efficiency `/v1/models`** | opencode: single `status_map()` | blackbox: per-model N full scans | BB-9. |
| DR-13 | **Stream resp leak on call-plan rejection** | nvidia (check pre-flight, release in finally) | opencode `main.py:494-498`; blackbox `main.py:405-409` | OC-2/BB-2. |

---

## 6. Security & config hygiene summary

| ID | Sev | Item | Status |
|----|-----|------|--------|
| SEC-1 | HIGH | `/dashboard` leaks `BEARER_TOKEN` unauthenticated (both wrappers) | OC-3 / BB-4 |
| SEC-2 | HIGH | blackbox runs with **empty BEARER_TOKEN** → fully open on loopback | BB-3 |
| SEC-3 | MED | `/metrics`, `/metrics/prom`, `/metrics/model-status`, `/health` unauthenticated — expose key labels, failure counts (no key material). Acceptable on loopback only. | Do not expose via reverse proxy without auth |
| SEC-4 | LOW | `metrics-snapshot.json` (both wrappers): contents verified = `{requests, tokens_in, tokens_out, errors, saved_at}` — **no secrets**, but untracked and **not gitignored**. | Add to `.gitignore` |
| SEC-5 | LOW | Bearer comparisons non-constant-time (both mains + `service.py:95`) | Use `hmac.compare_digest` |
| SEC-6 | LOW | Upstream exception text returned verbatim in 502 bodies — internal URLs/hostnames, no keys observed | Truncate/generalize |
| SEC-7 | INFO | Secret hygiene in persistence good: SHA-256 fingerprints only; `sanitize_error_detail` redacts bearer/sk/nvapi patterns | — |
| CFG-1 | MED | **F1 confirmed**: blackbox `.env` 0.0.0.0:9108 vs live 127.0.0.1:9104 | BB-10 |
| CFG-2 | LOW | `.gitignore`: `.env` ✅, `*.log` ✅, `*.db*` ✅; `metrics-snapshot.json` ❌ | Add pattern |
| CFG-3 | LOW | Orphaned stale `/root/wrapper/registry-state.db` (2.5 MB, last write ~2026-07-24) not used by running registry (MR-1); opencode `.env` lacks `MODEL_REGISTRY_URL` while blackbox has it | Consolidate |
| CFG-4 | INFO | Dead config: `ANTI_SILENCE_TIMEOUT_MS`, `REQUEST_TIMEOUT_MS` (opencode) | Remove |

---

## 7. Top remediation priorities (recommendation order — NOT applied)

1. **OC-1/BB-1** — replace copy-pasted `Mutex` with `asyncio.Lock` in all three key pools (whole-wrapper hang risk).
2. **SEC-1/SEC-2** — auth `/dashboard`, stop embedding the token; set blackbox `BEARER_TOKEN`.
3. **OC-2/BB-2 + MR-5** — move call-plan validation before the upstream call; release streaming responses on rejection.
4. **DR-1** — port nous `asyncio.wait_for` heartbeat to opencode/blackbox (and nvidia), line-aligned injection (OC-5/BB-6).
5. **DR-2** — wire `record_success/record_failure` in opencode/blackbox/nous so breakers actually function.
6. **CM-1** — fix `AliasResolver.resolve` scope-skip bug.
7. **MR-1/MR-2** — pin registry DB path, reconcile orphaned state file, fix loop-blocking/threadpool races.
8. Hygiene: gitignore `metrics-snapshot.json`, align blackbox `.env` (F1), delete dead config, boot-time catalog refresh.
