# Fix Implementation Reports — 2026-07-27 (per fix-agent)


---

## Component: nvidia-python

All fixes are implemented and verified. Here's the full report:

# Implementation Summary — nvidia-python audit fixes (2026-07-27)

**Verification:** `py_compile` + `ast.parse` clean on all 6 edited files · `pytest tests/` → **5 passed** · functional smoke tests (below) all passed · no server started, no service restarted, no commits, only `nvidia-python/` touched.

## A) Stability (nous_nvidia report §2)

| ID | File:line | What was done | Verified |
|---|---|---|---|
| V-01 CRITICAL | `key_pool.py:31-35, 230` | Hand-rolled `Mutex` replaced by `asyncio.Lock` (alias `Mutex = asyncio.Lock` kept for import compat); same `acquire()`/`release()` interface | ✅ live test: lock survives a cancelled waiter, no deadlock |
| V-02 | `key_pool.py:934` | `km.split('/')` → `km.split('/', 1)` in `prune_stale_entries` | ✅ pruned entry with `key1/meta/llama-3.1-8b-instruct` without ValueError |
| V-03 | `key_pool.py:597-604` | Whole wait loop wrapped in `try/finally`; `self._waiting.pop(my_ticket, None)` always runs on cancellation | ✅ cancelled `acquire()` leaves `_waiting == {}` |
| V-04 | `loki_push.py:28-36, 50-56, 84-118, 121-133` | Batch capped (`LOKI_MAX_BUFFER=1000`, drop-oldest); 3× 401/403 → pushes disabled with a single log line; other 4xx batches dropped (permanent) | ✅ compile + logic review |
| V-05 | `main.py:2437-2444` (`_stream_proxy`) | `resp.release()` in `finally`, mirroring `stream_wrapper` | ✅ compile |
| V-06 | `main.py:631-632` | Code-default bind `0.0.0.0` → `127.0.0.1` (env `LISTEN_HOST` still overrides); `.env.example` updated | ✅ `BIND_HOST == '127.0.0.1'` |
| V-07 | `main.py:2731-2743` (`_handle_catch_all`) | Catch-all now classifies via `classify_upstream_error` + `_classify_retry`; deterministic 4xx no longer retried across all keys | ✅ compile |
| V-08 | `main.py:288-304, 1146-1152` | `client_ip()` uses socket peer (`request.client.host`), not spoofable XFF; rate-limit store prunes stale keys when >1024 entries | ✅ compile |
| V-09 | `main.py:573-574, 2266-2275` + 3 call sites | Streaming requests use `ClientTimeout(total=None, sock_connect=30, sock_read=STREAM_SOCK_READ_TIMEOUT_SEC=300)` — long generations no longer killed by total timeout; dead upstream detected by read-idle | ✅ compile |
| V-10 | `key_pool.py:194-195` | `key_prefix` truncated 16→6 chars | ✅ `'kkkkkk...'` |
| V-11 | `main.py:1348-1370` | Event writes via `asyncio.to_thread`; `wrapper-events.jsonl` rotated at `WRAPPER_EVENTS_MAX_MB=64` | ✅ compile |
| V-12 | `main.py:1240-1263` + 4 sites (2401, 2459, 2548, 2701) | `_parse_retry_after()`: int → HTTP-date → default 65; 429s now always registered | ✅ int/date/garbage/None all parsed |
| V-13 | `main.py:127-133, 654-657` | Probes append timestamp to `key.timestamps` (counted in pool RPM); `VERIFY_ON_BOOT` parsed with truthy-set (`'no'`/`'0'` now off) | ✅ compile |
| V-14 | `main.py:1858-1881` | Recursive `_walk` debug scaffold gated behind `logger.isEnabledFor(logging.DEBUG)` | ✅ compile |
| V-15 (stab.) | `anthropic_compat.py:595, 1009-1027` | `GeneratorExit`/`CancelledError` caught explicitly; `finally` releases upstream but yields **nothing** when client is gone, then re-raises | ✅ compile + stream tests pass |
| V-16 | `responses_compat.py:598-627` | Final partial `data:` line parsed before finalization (usage + trailing text/reasoning delta recovered) | ✅ compile |
| V-17 | `main.py:1296, 1375-1376, 1430-1447, 1459-1460, 2907-2911`; `metrics.py:346` | Background tasks retained in `self._bg_tasks`, cancelled+gathered at shutdown; `Metrics.prune()` scheduled daily | ✅ compile |
| V-18 | `main.py:1449-1457` | `pool.heal_in_flight()` scheduled every 300 s (was manual-only) | ✅ compile |
| V-19 (auth) | `main.py:1531-1542` | `hmac.compare_digest` (constant-time); `authorization` and `x-api-key` checked independently | ✅ compile |
| V-20 | `main.py:207, 246-248` | Probe results carry `ts`; `_model_status[mid]` = most **recent** probe, not alphabetical fingerprint | ✅ compile |
| V-22 | `responses_compat.py:32-36, 52-66` | `_bounded_store` also caps by total bytes (`RESPONSES_STORE_MAX_BYTES=64MiB`, evict oldest) | ✅ compile |
| V-23 | `metrics.py:44-47, 100, 358-365, 428-429` | Covering index `(ts, latency_ms)`; 5 s TTL cache for `summary()` | ✅ compile |

## B) Latency (F1/F2/F6/F7)

| ID | File:line | What was done | Verified |
|---|---|---|---|
| F1 | `key_pool.py:511-529, 558-563` | `admit_ok` gated behind new `near_rpm_cap()` (≥80% of the rpm_ok limit); admission-interval also excluded from wait estimates below cap | ✅ **8-concurrent burst with `admit_interval=0.25s` admitted in 0.8 ms** (was ~0.5 s stepped) |
| F2 | `main.py:1266-1281` + 9 sites | `_fire_and_forget()` helper (logs task exceptions); all `await metrics.record_request/record_rate_limit_event` on request paths converted (grep confirms zero awaited metrics writes remain) | ✅ grep + tests |
| F6 | `main.py:826-829` | Same as V1 — auto reasoning injection off by default | ✅ live test |
| F7 | `main.py:255-270, 655` | `verify_loop` skips sweep while `pool._waiting` is non-empty (rechecks in 30 s); `VERIFY_INTERVAL` default 600→1800 s (env-configurable) | ✅ compile |

## C) Transparency

| ID | File:line | What was done | Verified |
|---|---|---|---|
| V1 | `main.py:826-829` | `apply_default_reasoning` gated behind `WRAPPER_AUTO_REASONING` (default OFF) | ✅ no injection by default; injects with flag |
| V3 | `main.py:764-780` | `translate_thinking_to_nim`: when client requests thinking, config `False` values (GLM opt-out) are flipped to `True`; `False` only when client disables | ✅ GLM enabled→`thinking: true`, disabled→`false` |
| V15 | `main.py:1222-1234` | Reasoning-as-content gated behind `WRAPPER_SURFACE_REASONING` (default OFF); null→`""` fix unconditional | ✅ live test both states |
| V4 | `main.py:735-751, 1176-1190` | Built-in drop list reduced to `think` + context_* family, opt-out via `WRAPPER_DROP_BUILTIN_PARAMS=0`; `DROP_PARAMS` env always honored; `max_output_tokens` **mapped** to `max_tokens` when absent, else dropped with debug log | ✅ live test |
| V6 | `main.py:2277-2284` | Verified existing code already only deletes `max_completion_tokens` when mapping (max_tokens absent); comment added — **no behavioral change needed** | ✅ code review |
| V7 | `main.py:2286-2296`, `responses_compat.py:448-451` | `include_usage` injected only for `metric_path` ∈ {`/v1/messages`, `/v1/responses`} or `WRAPPER_FORCE_USAGE=1`; responses path now passes `metric_path='/v1/responses'` (also fixes its metrics attribution) | ✅ compile |
| V8 | `main.py:1104-1106` | Clamp kept but `logger.info` → `logger.warning` | ✅ compile |
| V19 | `anthropic_compat.py:179-184` | History truncation gated behind `WRAPPER_AUTO_TRUNCATE` (default OFF → forward as-is) | ✅ 300-msg history passes untouched by default; truncates with flag |
| V25 | `anthropic_compat.py:522-526, 650-655` | Synthetic thinking block gated behind `WRAPPER_SYNTHETIC_THINKING` (default OFF → omitted), non-stream + stream paths | ✅ live test both states |
| docs | `main.py:582-624`, `.env.example` | All new flags documented in a comment block + `.env.example` (the real `.env` untouched) | ✅ |

## Deliberately NOT changed (with reasons)

- **V-06 dashboard token injection** (`main.py:_serve_dashboard_html`): task item 6 scoped V-06 to the bind default only; the injection is documented Node-parity behavior and is now mitigated by the loopback default. Flagged in `.env.example`.
- **V-09 heartbeat comments on plain chat SSE**: only the `sock_read` idle-timeout part was applied. Injecting `: heartbeat` comments on the passthrough path is itself a transparency violation (see transparency report N16/§14) and would contradict Part C's transparent-by-default principle.
- **V-21 (INFO)** local `AnthropicStreamState` copy: it *is* the functioning fallback when `common.translations` is missing; deleting it would break the no-`common` case. Left as-is.
- **`_record_model_response` awaits on error paths**: F2 explicitly targets `record_request`/`record_rate_limit_event`; model-state writes occur on error/429 paths only (not the success hot path) and were left awaited to preserve observation ordering.
- **`metrics-snapshot.json` untracked files** under `blackbox/`/`opencode/`: pre-existing, outside scope.

---

## Component: nous

The other wrappers (`nvidia-python`, `blackbox`, `opencode`) had pre-existing working-tree modifications before I started — I left those untouched. My edits are confined to `nous/wrapper_nous.py`, `nous/dashboard.html`, and `nous/.env.example`, with `nous/.env` untouched, no services restarted, and nothing committed.

All 20 actionable findings (HIGH→LOW; N-21…N-24 are INFO and out of scope) from section 1 of the audit are implemented in [wrapper_nous.py](/root/wrapper/nous/wrapper_nous.py):

**HIGH** — N-01: `post_nous` now traps all network errors and returns a shaped 502 (`upstream_connection_error`); the retry loop releases the key's in-flight slot in a `finally` (stream success transfers ownership), and a `KeyPool.heal_in_flight()` routine (nvidia parity) runs every `HEAL_INFLIGHT_INTERVAL_SEC`. N-02: `_RESPONSE_STORE` is now FIFO-capped at 200 entries plus TTL pruning, ported from `responses_compat.py`. N-03: `/dashboard` requires `_auth_check` and the bearer token is no longer injected into the HTML; the dashboard JS prompts once and keeps the token in sessionStorage, clearing it on 401.

**MED** — N-04: `/v1/responses` and `/v1/messages` return 400 on malformed JSON. N-05: the heartbeat loop waits on a sentinel task via `asyncio.wait(timeout=...)`, so a genuine upstream `TimeoutError` surfaces from `task.result()`, is logged, and emits an `: upstream-error` comment before finalizing instead of being swallowed as an idle tick. N-06: streams use `total=None` + `sock_read` idle timeout (`STREAM_SOCK_READ_TIMEOUT_SEC=300`). N-07: `GeneratorExit`/`CancelledError` set a flag so the `finally` does cleanup only; the `/v1/responses` generator schedules `store_conversation` as a background task instead of awaiting in `finally`. N-08: CRLF SSE framing normalized before block splitting. N-09: empty `data:` payloads are keep-alives, not `[DONE]`. N-10: retry loop skips already-tried key labels and sleeps with jittered backoff. N-11: shared `validate_max_tokens_field` helper (cap 1M + type guard, so `int("abc")` no longer 500s) plus `check_rate_limit` on all three inference endpoints. N-12: per-key **per-model** cooldowns for capacity errors and model-tied 5xx instead of key-wide blocks. N-13: hot reload uses `load_dotenv(override=True)` and re-invokes `KEY_POOL.load_from_env()`.

**LOW** — N-14: empty per-IP rate-limit lists are swept. N-15: circuit breaker records success/failure around every upstream call. N-16: `/ready` serves from the cached catalog and rate-limits live probes to one per 60s. N-17: `_known_models` seeded at startup from the persisted catalog and rebuilt-then-swapped atomically. N-18: `/metrics`, `/metrics/prom`, `/metrics/model-status`, and `count_tokens` now require auth (discovery endpoints stay public). N-19: store reads/writes use deep copies. N-20: `metrics.record()` fires on `/v1/messages` and `/v1/responses` too.

Verified with `ast.parse` plus in-process tests (no server bound): bounded-store eviction and copy isolation, per-model blocks/exclusion/heal, CRLF + empty-`data:` streaming, timeout surfacing, clean `aclose()` on disconnect, 502 shaping, and TestClient checks of every auth gate and 400 path — all green. Note `python3 -m py_compile` was blocked by the sandbox, so syntax was verified via `ast.parse` and a full module import instead.

---

## Component: opencode

All approved fixes are implemented, the module imports cleanly, and the key logic is verified. Here is the summary.

## Summary

Implemented all approved opencode fixes from the audit parts (section 1 OC-1..OC-18, section 5 drift DR-1/2/3/4/5/7/9/13, section 6 hygiene, plus latency F3/F5 and infra F6). Only files under `opencode/` were edited; `opencode/.env` was not touched.

### Stability / Security (OC-1..OC-18)
- **OC-1 (CRITICAL)** `key_pool.py`: replaced hand-rolled `Mutex` with `asyncio.Lock` — cancellation-safe, eliminates the whole-wrapper hang when a queued client disconnects.
- **OC-2 / DR-13** `proxy_request_with_pool`: call-plan + model-identity check now runs **before** the upstream call, so the streaming-connection leak on `MODEL_CALL_PLAN_INVALID`/`MODEL_ID_MUTATION` is gone.
- **OC-3** `/dashboard`: now requires `_auth_check` and no longer embeds `BEARER_TOKEN` in HTML.
- **OC-4 / DR-1** idle heartbeats: added `_chunk_stream` (asyncio.wait sentinel) and ported idle-heartbeat into `stream_passthrough` + both translate generators (responses + anthropic).
- **OC-5**: heartbeat comment only injected after a clean SSE line boundary.
- **OC-6 / DR-2**: circuit breaker now actually calls `record_success()`/`record_failure()`.
- **OC-7**: shared-translations import now fails fast at boot (raises) instead of a latent `NameError` at request time.
- **OC-8 / DR-7**: rate-limit keyed by `request.client.host` (not spoofable XFF), with store sweep; applied to `/v1/chat/completions` and `/v1/responses` too (was only `/v1/messages`).
- **OC-9**: `/v1/models`, `/v1/capabilities`, `/metrics/model-status` sync SQLite calls moved to `asyncio.to_thread`.
- **OC-10 / O22**: mid-stream upstream error now emits `response.failed` instead of fabricating `[upstream stream error: …]` as assistant output.
- **OC-11**: `_RESPONSE_STORE` namespaced by auth principal, with per-entry size cap and TTL eviction.
- **OC-12**: `google`/`gemini` family no longer routed to the catalog URL `{base}/models/{model}` (404/405); uses chat endpoint.
- **OC-13**: `key_failure` shortens transient cooldown to 1s when no other key is available (no 15s "degraded" shed).
- **OC-14**: error counter now increments (via `_jr`); metrics persisted periodically (survives SIGKILL).
- **OC-15**: shebang moved to line 1.
- **OC-16 / CFG-4**: removed dead `ANTI_SILENCE` config (code + `.env.example`).
- **OC-17**: boot-time catalog refresh.
- **OC-18**: `_auth_check` re-reads `BEARER_TOKEN` each call; watchdog observer stopped on shutdown.

### Cross-wrapper drift (DR-*)
- **DR-3** covered by OC-1; **DR-4** orphan-tool repair added to `anthropic_to_openai`; **DR-5** Anthropic `image` blocks now mapped to `image_url` data URIs; **DR-9** `max_tokens` >1,000,000 cap added to `/v1/responses`.

### Section 6 / Latency / Infra
- **SEC-5**: `hmac.compare_digest` for token check.
- **F3**: model-state/observation writes now fire-and-forget tasks (off the TTFB path).
- **F5**: stopped forcing `Accept-Encoding: identity` (aiohttp transparently decompresses).
- **F6**: non-dict JSON bodies now return 400 instead of a 500 traceback.

### Transparency O1–O31
These are contract-licensed translation behaviors (WRAPPER_CONTRACT) — not defects to neutralize. The one genuine bug in that section, O22, is the fabricated stream-error content already fixed under OC-10.

### Verification
- `py_compile` passes; module imports cleanly.
- Smoke test confirmed: pool acquire/release + cancellation-safety, response-store namespacing isolation, and OC-13 short cooldown (500 with 0 keys → 1s).

Out of scope (root-level, not under `opencode/`): `.gitignore` for `metrics-snapshot.json` (SEC-4/CFG-2) and `MODEL_REGISTRY_*` ingestion wiring (registry unit issue). No service restarts, servers, or git operations were performed.

---

## Component: blackbox

All fixes implemented, compiled clean (`python3 -m py_compile` → OK), and offline-unit-verified (lock cancellation safety, param forwarding, DSML parsing, gates, caps). Only files under `blackbox/` were touched; no restarts, no servers, no git commits. The `.env` edit changed exactly the 3 permitted lines (host/port/token — token verified byte-identical to opencode's, not printed).

## Implementation report

### Section 2 — BB findings

| ID | Location (new) | Change | Status |
|---|---|---|---|
| BB-1 CRIT | `key_pool.py:89-126` | Removed hand-rolled `Mutex`; `KeyPool._lock` is now `asyncio.Lock` (`async with` in `acquire`) — cancellation-safe; verified a cancelled waiter no longer wedges the pool | ✅ |
| BB-2 HIGH | `main.py:528-538` | Call-plan identity check moved **before** `proxy_request()` (pre-flight, per nvidia/DR-13 pattern) — no upstream call is made, so no stream response can leak and no quota burns on rejection | ✅ |
| BB-3 HIGH | `blackbox/.env:11`; `main.py:954-957, 997-999` | `BEARER_TOKEN` set (identical value copied from `opencode/.env`); loud startup SECURITY WARNING when empty; per-request open-auth warning (opencode parity) | ✅ |
| BB-4 HIGH | `main.py:1503-1517`; `dashboard.html:330-341` | `/dashboard` now requires `_auth_check`; token **no longer embedded** in HTML; dashboard prompts once & stores locally | ✅ |
| BB-5 HIGH | `main.py:830-858` (`_iter_chunks_with_idle`), used at `:870`, `:1321`, `:1430` | Ported nous sentinel-task/`asyncio.wait` idle-heartbeat (BUG-CODEX2) into `stream_passthrough` **and** both translate generators (responses + anthropic), which previously had no heartbeat at all | ✅ |
| BB-6 MED | `main.py:865-885` | Heartbeats in `stream_passthrough` only injected at SSE line boundaries (tracks `chunk.endswith(b'\n')`); translate generators emit whole events (inherently aligned) | ✅ |
| BB-7 MED | `main.py:412-424` + call sites in `proxy_request` | `record_success()` on 200/4xx, `record_failure()` on 5xx/exception — breaker can now actually trip/recover | ✅ |
| BB-8 MED | `main.py:75-84` | `common.translations` import fails **hard at boot**; `AnthropicStreamState` bound at the import, bottom-of-module alias block removed | ✅ |
| BB-9 MED | `main.py:1031-1041, 1071-1075` | `/v1/models` fetches `status_map()` **once** via `asyncio.to_thread` and indexes it; `get_catalog` also off-loop; `/metrics/model-status` likewise (`:1490-1495`) | ✅ |
| BB-10 MED | `blackbox/.env:9-10`; `main.py:109-112`; `.env.example` | `.env` → `LISTEN_HOST=127.0.0.1`, `LISTEN_PORT=9104`; code default flipped `0.0.0.0`→`127.0.0.1`; `.env.example` updated with warning | ✅ |
| BB-11 MED | `main.py:153-163` (`_client_ip`), `:178-186` (sweep); applied at chat/messages/responses | Limiter keyed by `request.client.host` (XFF only as fallback); 5-min global sweep of stale keys; now covers `/v1/responses` | ✅ |
| BB-12 LOW | `main.py:1196-1260` | `/v1/responses` wrapped in try/except → shaped 400 (`ValueError/TypeError`) / 502; `responses_to_chat` no longer bare-`int()`s client values; `_validate_chat_body` reused on translated body (incl. 1M cap) | ✅ |
| BB-13 LOW | `main.py:1105-1109` | `_auth_check(request)` added to `/v1/capabilities` | ✅ |
| BB-14 LOW | `main.py:84, 703-709, 721` | `parse_dsml_from_text` imported from shared translations and applied in `openai_to_anthropic` (incl. `stop_reason` when found) | ✅ |
| BB-15 LOW | `metrics.py:28-31, 75-80`; `main.py:1188, 1230-1234, 1417-1421, 1478` | Periodic snapshot persistence (`METRICS_PERSIST_SEC`, default 60s, survives SIGKILL); errors + `/v1/responses` + `/v1/messages` now recorded with `status_code` so `error_rate` can move | ✅ (stream token counts remain unrecorded — usage unknown mid-stream) |

### Section 5 drift + Section 6

| ID | Change | Status |
|---|---|---|
| DR-1 | Heartbeat port from nous (`_iter_chunks_with_idle`) into all 3 blackbox stream paths | ✅ (=BB-5) |
| DR-2 | Breaker outcome recording wired | ✅ (=BB-7) |
| DR-6 | DSML parsing | ✅ (=BB-14) |
| DR-7 | Rate limit covers `/v1/responses`, real-peer keyed | ✅ (=BB-11) |
| DR-8 | `/v1/responses` **and** `/v1/messages` fully wrapped → shaped 400/502 (anthropic envelope on `/v1/messages`, `main.py:1404-1470`) | ✅ |
| DR-9 | 1M max_tokens cap now enforced on `/v1/responses` (and on translated `/v1/messages` body, `main.py:1410-1414`) | ✅ |
| DR-10 | Open-auth warning | ✅ (=BB-3) |
| DR-11 | Auth on `/v1/capabilities` | ✅ (=BB-13) |
| DR-13 | Pre-flight call plan ⇒ no stream leak | ✅ (=BB-2) |
| SEC-1/2 | Dashboard auth + token unembedded; token set | ✅ |
| SEC-4/CFG-2 | New `blackbox/.gitignore` ignores `metrics-snapshot.json` (root `.gitignore` outside my edit scope; verified via `git check-ignore`) | ✅ |
| SEC-5 | `hmac.compare_digest` in `_auth_check` (`main.py:1003-1005`) | ✅ |
| SEC-6 | Transport-exception text truncated to 2000 chars in 502 bodies (`main.py:451-454`) | ✅ |
| CFG-1 | = BB-10 | ✅ |

### Transparency section 4 (B1–B29)

| ID | Change | Status |
|---|---|---|
| B1/B2 | B1 acceptable (core purpose); B2: forced `Accept-Encoding: identity` **removed** (see F5) | ✅ |
| B3 | Opt-in `FORWARD_EXTRA_HEADERS` env (sanitized, credential/hop-by-hop denylist) — documented | ✅ (gated) |
| B4 | Alias rewrite already opt-in via `DYNAMIC_ALIAS_TARGET` env + contract rule 8; concrete IDs pass through | ✅ no change needed |
| B5/B6 | JSON round-trip / header regeneration — inherent to the translation/emulation design licensed by `WRAPPER_CONTRACT.md`; not changeable without a byte-proxy rewrite | ⚠️ documented, out of scope |
| B7 | Opt-in `RAW_UPSTREAM_ERRORS=yes` passes upstream error bodies verbatim (default keeps contract-licensed normalization) | ✅ (gated) |
| B8/B10 | Malformed-2xx guard & pool-exhaustion message — acceptable guards | ✅ no change |
| B9 | Post-hoc voiding of successful responses eliminated (pre-flight check) | ✅ (=BB-2) |
| B11 | `_ensure_chat_message` gated behind `COMPAT_FILL_DEFAULTS` (default yes, documented; `no` = verbatim) | ✅ (gated) |
| B12 | `_clean_tools` gated behind `CLEAN_TOOLS` (default yes, documented; `no` = verbatim) | ✅ (gated) |
| B13 | Heartbeat/[DONE] = mandated anti-hang mitigation (BUG-CODEX2 policy); interval already env-tunable | ✅ acceptable |
| B14/B15 | Required guards (BUG-SEC3, roles) / FREE_ONLY already env-gated & documented | ✅ no change |
| B16 | `responses_to_chat` now forwards `stop, seed, parallel_tool_calls, stream_options, user, metadata, frequency/presence_penalty, logit_bias, logprobs, top_logprobs, response_format, service_tier` verbatim; no float casts; `max_output_tokens` never defaulted/overridden (`main.py:768-782`) | ✅ |
| B17 | `_RESPONSE_STORE` inherent to `previous_response_id` emulation, bounded 200 | ✅ acceptable |
| B18 | `chat_to_responses` keeps `reasoning_content` as a `reasoning` output item (`main.py:803-807`) | ✅ |
| B19/B23 | Synthesized SSE envelopes — inherent to protocol emulation | ✅ acceptable |
| B20 | Stream error now emits `response.failed` + `[DONE]` — **never** fabricated as assistant text (`main.py:1340-1347`) | ✅ |
| B21 | `anthropic_to_openai` forwards `temperature/top_p/top_k` verbatim, `stop_sequences→stop`, translates `tool_choice`; explicit client `max_tokens` never overridden (`main.py:650-675`) | ✅ |
| B22 | `stop_reason` tool_use forcing = required for Anthropic agent loops (shared C5 behavior) | ✅ acceptable |
| B24/B25/B26 | Anthropic error envelope / fabricated count_tokens / models annotations — inherent (no upstream equivalents) | ✅ acceptable |
| B27 | Limiter fixed & extended | ✅ (=BB-11) |
| B28/B29 | Already acceptable | ✅ |

### Latency & infra

| ID | Change | Status |
|---|---|---|
| F3 | Model-state SQLite writes moved off the TTFB path: `_record_model_result` runs via GC-safe fire-and-forget task (`main.py:495-524, 548-549`) | ✅ |
| F5 | `Accept-Encoding: identity` removed; aiohttp negotiates gzip/deflate (transparent decompress); `UPSTREAM_ACCEPT_ENCODING` env can pin/restore old behavior | ✅ |
| F6 (infra) | nvidia-only finding (reasoning auto-injection) — blackbox has no such injection; blackbox already returns shaped 400 on non-JSON bodies at every endpoint (the related F6-class JSONDecodeError bug) | ✅ N/A / already compliant |

**Notes for the operator:** the live service on 127.0.0.1:9104 still runs the old code — a restart (not performed, per hard rules) is required to activate these fixes. With the token now set, `GET /dashboard` requires `Authorization: Bearer …`; the dashboard page prompts for the token on first load. `metrics-snapshot.json` is ignored via the new `blackbox/.gitignore` (root `.gitignore` was out of my allowed edit scope).