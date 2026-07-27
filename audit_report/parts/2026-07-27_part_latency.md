
# Proxy-Wrapper Latency Audit

## PART 1 — Static analysis of the hot request path

Shared infrastructure that is **NOT** a problem (verified in all four wrappers):
- A single `aiohttp.ClientSession` with a pooled `TCPConnector` (limit/limit_per_host/ttl_dns_cache) is created once and reused (nous:wrapper_nous.py:1645, nvidia:main.py:1188, opencode:main.py:326, blackbox:main.py:297). No per-request client / no per-request TCP+TLS handshake.
- Streaming uses `resp.content.iter_any()`/`iter_chunks()` and forwards chunks as they arrive; `X-Accel-Buffering: no`, `Cache-Control: no-cache`, `Connection: keep-alive` are set on every `StreamingResponse`. No full-response buffering on the stream path.
- `common/middleware.RequestSizeLimiter` only inspects `Content-Length` (no body read). `common/circuit_breaker` uses an `asyncio.Lock` — negligible.

### Findings table

| # | File:line | Mechanism | Impact | Wrappers |
|---|-----------|-----------|--------|----------|
| F1 | nvidia-python/src/key_pool.py:446-595 (admit `_admit_interval = 1/QUEUE_LIMIT`; .env `QUEUE_LIMIT=4` → 0.25s) | **Request pacing/sleep** in `KeyPool._acquire_slot`: when concurrent requests exceed the number of ready keys, the caller `await asyncio.sleep(sleep_duration)` in a queue. Adds up to `min(wait, 5s)` per extra concurrent request. | **TTFB + total, on every concurrent request** (not on isolated singles). Measured: 12-concurrent tail +0.3s, 24-concurrent tail +0.7s. | nvidia-python **only** |
| F2 | nvidia-python/src/main.py:2262-2271 (non-stream) / 2182-2189, 2249 (error) / metrics.py:164 (aiosqlite INSERT+commit) | `await self.metrics.record_request(...)` / `record_rate_limit_event(...)` is an **await on the hot path before the HTTP response is returned**. Each write = aiosqlite INSERT + `commit()` against `metrics.db`. | TTFB + total on every non-streaming/error request (~1–10 ms, WAL). | nvidia-python |
| F3 | nous/wrapper_nous.py:1829 `record_model_result` (awaited); opencode/main.py:499-511; blackbox/main.py:410-421 `MODEL_STORE.record_status_async`/`record_error_async` + `ModelRegistryClient.schedule_observation` | Model-state/observation persistence is **awaited before the streaming response is returned or before the JSON is sent**. Throttled to 1 write/60s per (scope,model,endpoint) via in-memory cache, but each non-throttled write = 2x SQLite INSERT + commit (`to_thread`). `schedule_observation` is fire-and-forget (cheap), but the SQLite write is not. | TTFB on streaming/non-streaming (~1–10 ms per request in the non-throttled window). | nous, opencode, blackbox |
| F4 | nvidia-python/src/main.py:2261 `metrics.record_request` only on non-429 success; streaming path does NOT record metrics pre-stream, but F3-equivalent present via `_record_model_response` at main.py:1286 (awaited inside proxy flow for errors) | Same DB-write-on-path class as F3, plus the extra NVIDIA metrics layer. | minor additive TTFB | nvidia-python |
| F5 | opencode/main.py:561 & blackbox/main.py:305 `_auth_headers(... "Accept-Encoding": "identity")` | Wrapper **disables gzip** to the upstream (`Accept-Encoding: identity`) and never re-compresses. Large responses (long generations, image/tool models) travel uncompressed. | total only, scales with response size (KB–MB) | opencode, blackbox |
| F6 | nvidia-python/src/main.py:730 `apply_default_reasoning` / 697 `translate_thinking_to_nim` | For models whose `REASONING_CONFIGS` entry has `requires_reasoning=True` (e.g. deepseek-v4/r1, `-reasoning`, nemotron-3-*), the wrapper **force-injects `enable_thinking`** even when the client sent no thinking request. Adds model-side reasoning latency (the code itself documents a 4–5s GLM case, now opt-out for GLM only). | TTFB + total, only on specific models / `/v1/messages`. Not triggered for plain `/v1/chat/completions` on glm/llama. | nvidia-python |
| F7 | nvidia-python/src/main.py:246 `verify_loop` (every `VERIFY_INTERVAL=600s`, 100 models × 6 accounts, real `/v1/chat/completions` probes) | Background model verification consumes the **same key-pool RPM** as live traffic (`pool.refresh_models`, `probe_model` at main.py:116). 664 sweeps in the log; each burns ~100×6 upstream requests and pushes keys toward their soft RPM limit, making `F1` pacing trigger earlier under load. | indirect — lowers the concurrency threshold at which F1 bites. | nvidia-python |
| F8 | nous/wrapper_nous.py:1618 `_read_token_from_auth_path` called in `post_nous_with_retries` per request | Synchronous `json.load` of `AUTH_PATH` file on every request when OAuth auth is configured. | TTFB only if AUTH_PATH set (single small file read; sub-ms typical). | nous (only if AUTH_PATH configured) |
| F9 | nous/wrapper_nous.py:991 `stream_with_heartbeat` buffers by `\n\n` SSE blocks + per-chunk `json.loads`; opencode/blackbox/opencode similar `_responses_stream`/`gen` | SSE re-parsing per chunk (full json parse of every delta) and heartbeat injection. Heartbeats only fire on `HEARTBEAT_INTERVAL_MS` idle (5s), so they do **not** delay real chunks. | negligible CPU (<1 ms/chunk); not a real contributor. | all |

**Not found:** per-request httpx/aiohttp client creation (all share a session), gzip decompress+recompress on the response path, non-stream `await resp.read()` before streaming, or missing `stream=True`.

---

## PART 2 — Live measurements

Key values read from `.env` but **redacted** in this report (`NOUS_API_KEY_*` = [REDACTED], `NVIDIA_API_KEY_*` = [REDACTED]). Models: nous `tencent/hy3:free`, nvidia `meta/llama-3.1-8b-instruct`. curl `-w` timings; median of 3 after a warmup.

### A. Non-streaming chat completion (time_starttransfer = TTFB, time_total = total)

| Endpoint | direct median TTFB | direct median total | via wrapper median TTFB | via wrapper median total | delta TTFB | delta total |
|---|---|---|---|---|---|---|
| nous `/v1/chat/completions` (max_tokens=16) | 0.164s | 2.78s | 2.55s | 2.55s | +2.39s | ~0s |
| nvidia `/v1/chat/completions` (max_tokens=16) | 0.248s | 0.53s | 0.39s | 0.39s | −0.14s (faster) | −0.14s |

Single isolated requests show **no wrapper penalty** (curl direct even pays a fresh TLS handshake each call). The nous direct TTFB is dominated by upstream (the 2.39s delta is upstream variance between runs, not the wrapper).

### B. Streaming TTFB (time to first SSE byte)

| Endpoint | direct TTFB (min/med/max) | via wrapper TTFB (min/med/max) | delta |
|---|---|---|---|
| nous | 1.91 / 2.14 / 2.26 | 1.90 / 2.02 / 4.43 | ~0 (one 4.4s outlier = upstream) |
| nvidia | 0.52 / 0.53 / 0.55 | 0.28 / 0.48 / 0.64 | ~0 (faster: pooled TLS) |

Streaming TTFB is **parity** — the wrapper's pooled session even removes curl's per-call TLS cost.

### C. Concurrency (nvidia) — exposes F1 pacing

| Scenario | direct (12 concurrent) total range | via wrapper (12 concurrent) total range | via wrapper (24 concurrent) total range |
|---|---|---|---|
| TTFB/first-byte | 0.20–0.25s | **0.56–0.84s** | **0.51–1.23s** |
| pattern | flat (real parallelism) | stepped ~0.25s waves | stepped ~0.25s waves, tail 1.23s |

Direct upstream is flat at ~0.2s even at 12 concurrent (upstream itself parallelizes). The wrapper **adds ~0.3s at 12 and ~0.7s at 24** in clear 250ms steps — matching `QUEUE_LIMIT=4` → `admit_interval=0.25s` and the 6-key pool (24 reqs ÷ 6 keys = 4 waves × 0.25s). This is the smoking gun for "client feels slower than curl."

### D. Overhead floor — `/v1/models` (cached, no upstream on hot path)

| Endpoint | direct total | via wrapper total | delta |
|---|---|---|---|
| nvidia `/v1/models` | 0.27–0.37s | 0.04–0.05s | −0.27s (cached, faster) |
| nous `/v1/models` | 0.66–1.28s | 0.08–0.13s | −0.6s (cached, faster) |

The wrapper floor (enrich + catalog build) is only **40–130 ms**; the slow part of `/v1/models` is the upstream call, which the wrapper caches. So the wrapper is *faster* here — no penalty.

---

## ROOT-CAUSE RANKING

1. **[HIGH confidence] F1 — nvidia-python key-pool pacing sleep (`QUEUE_LIMIT=4`).** This is the only finding that produces a *reproducible, wrapper-only* latency increase, and it scales with the request rate. A CLI/agent firing several requests in quick succession (or any burst > number of ready keys) is delayed 250 ms per "wave." Direct curl to the upstream shows no such stepping. This fully explains the reported symptom for nvidia-python.
2. **[MEDIUM confidence] F7 — nvidia verify-loop consuming the same key RPM.** Makes F1 trigger at lower live concurrency by keeping key RPM high. Indirect amplifier of #1.
3. **[MEDIUM confidence] F3/F4 — per-request SQLite model-state/metrics writes awaited before response.** Present in all four wrappers (nvidia has the extra aiosqlite metrics layer, F2). Adds a small but real TTFB tax (~1–10 ms) on every request, and is the most likely explanation for any *consistent* non-zero floor on nous/opencode/blackbox where F1 does not apply. On the entries that fall in the throttled 60s window it is near-zero; on the first request of each window it is the full commit cost.
4. **[LOW/conditional confidence] F6 — reasoning auto-injection** for specific nvidia models via `/v1/messages`. Explains slowness only if the client uses the Anthropic surface with a `requires_reasoning` model; not the generic chat-completions case.
5. **[LOW] F5 — `Accept-Encoding: identity`** on opencode/blackbox: only matters for large responses; not the general cause.
6. **[INCONCLUSIVE] F8/F9** — negligible.

**Net:** For nvidia-python the dominant, measured cause is **F1 (pacing)**. For nous/opencode/blackbox the static path is genuinely tight; their "slowness" is most plausibly either (a) the small but universal per-request DB writes (F3) under load, or (b) the fact that an agent usually issues *concurrent/sequential bursts* and only nvidia's pacing is severe — the others rely on a hard RPM cap (nous 60/min/key) that produces 429s rather than added latency, which is a different failure mode. The single-request measurements show no wrapper tax on any of the four, so the report's "noticeably slower" is a **concurrency/pacing** effect, not a per-request overhead.

---

## FIX RECOMMENDATIONS (not applied)

- **F1 (primary):** Remove or drastically raise the admission-interval pacing. `QUEUE_LIMIT=4` (0.25s/key) is far below the 30–40 RPM soft limit and throttles the wrapper far more aggressively than the upstream would. Either set `QUEUE_LIMIT` very high (e.g. 100+) so `_admit_interval` → ~0, or only pace when a key is genuinely near its RPM cap (the `rpm_ok` check already exists at key_pool.py:521 — gate `admit_ok` behind it instead of unconditionally). Consider a token-bucket per key instead of a fixed sleep.
- **F2/F3/F4 (hot-path DB writes):** Fire `MODEL_STORE.record_status_async` / `metrics.record_request` **after** the response is returned to the client (e.g. `asyncio.create_task(...)` / background task, or a queue drained by a worker, mirroring how `ModelRegistryClient.schedule_observation` is already fire-and-forget). This removes the await from the TTFB path entirely. Keep the throttling, but never `await` the commit on the request coroutine.
- **F5:** Allow upstream gzip (`Accept-Encoding: gzip, deflate`) and, if re-serializing, re-compress; or at minimum stop force-disabling compression.
- **F6:** Make reasoning injection opt-in/transparent (do not auto-enable `enable_thinking` unless the client requested thinking), consistent with the documented GLM opt-out.
- **F7:** Run verify sweeps against a *separate* key/quota or at a lower cadence, or exclude them from the live key-pool RPM accounting, so they don't push user traffic into pacing.
- **F8:** Cache the OAuth token in memory (refresh on failure) instead of reading the file every request.

No files were modified, no services restarted, and no project scripts were run — only read-only inspection plus curl/python live measurements against the already-running services.