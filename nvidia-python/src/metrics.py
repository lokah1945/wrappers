#!/usr/bin/env python3
"""
metrics.py — SQLite-backed request metrics.
Migrated from metrics.js — functionally identical.

Tables: requests, model_status, rate_limit_events

Provides:
  - record_request()           — log a completed request
  - record_rate_limit_event()  — log a 429 event
  - set_model_status()         — log model verification status
  - summary()                  — aggregate stats for dashboard
  - get_per_model()            — per-model breakdown
  - get_per_key()              — per-key breakdown
  - get_hourly_chart()         — hourly timeseries
  - get_daily_chart()          — daily timeseries
  - get_model_timeseries()     — per-model hourly timeseries
  - reset_all()                — clear all data
  - prune()                    — delete old data
"""

import time
import asyncio
import aiosqlite
import logging

logger = logging.getLogger('wrapper-nvidia')

WINDOW_SECS = {
    '1m': 60, '5m': 300, '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000,
}


class Metrics:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection = None
        self._write_counter = 0
        self._ready = asyncio.Event()
        self._on_request = None
        self._on_rate_limit = None
        self._save_interval = None

    def on_request(self, cb):
        self._on_request = cb

    def on_rate_limit(self, cb):
        self._on_rate_limit = cb

    async def init(self):
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute('PRAGMA journal_mode=WAL')

        # Schema migration: check for old schema
        try:
            cursor = await self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
            )
            row = await cursor.fetchone()
            if row:
                cursor = await self._db.execute("PRAGMA table_info(requests)")
                cols = await cursor.fetchall()
                col_names = [c[1] for c in cols]
                if 'total_tokens' not in col_names:
                    logger.info('[metrics] Migrating schema: adding total_tokens column...')
                    await self._db.execute("ALTER TABLE requests ADD COLUMN total_tokens INTEGER DEFAULT 0")
                if 'ttft_ms' not in col_names:
                    logger.info('[metrics] Migrating schema: adding ttft_ms column...')
                    await self._db.execute("ALTER TABLE requests ADD COLUMN ttft_ms REAL DEFAULT 0")
        except Exception as e:
            logger.warning(f'[metrics] Schema check failed: {e}')

        await self._db.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                REAL    NOT NULL,
                method            TEXT,
                path              TEXT,
                model             TEXT,
                key_label         TEXT,
                streaming         INTEGER DEFAULT 0,
                status_code       INTEGER,
                latency_ms        REAL    DEFAULT 0,
                prompt_tokens     INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cached_tokens     INTEGER DEFAULT 0,
                total_tokens      INTEGER DEFAULT 0,
                was_rate_limited  INTEGER DEFAULT 0,
                retries           INTEGER DEFAULT 0,
                request_bytes     INTEGER DEFAULT 0,
                pacing_ms         REAL    DEFAULT 0,
                ttft_ms           REAL    DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_req_ts    ON requests(ts);
            CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);
            CREATE INDEX IF NOT EXISTS idx_req_key   ON requests(key_label);
            CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status_code);
            CREATE INDEX IF NOT EXISTS idx_req_ts_model ON requests(ts, model);
            CREATE INDEX IF NOT EXISTS idx_req_ts_key ON requests(ts, key_label);

            CREATE TABLE IF NOT EXISTS model_status (
                model       TEXT PRIMARY KEY,
                ok          INTEGER,
                last_status INTEGER,
                reason      TEXT,
                endpoint    TEXT,
                checked_at  REAL
            );

            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             REAL NOT NULL,
                key_label      TEXT,
                model          TEXT,
                retry_after_s  INTEGER,
                detected_limit INTEGER,
                rotated_to     TEXT,
                scope          TEXT DEFAULT 'key',
                observed_rpm   INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_rl_ts     ON rate_limit_events(ts);
            CREATE INDEX IF NOT EXISTS idx_rl_key    ON rate_limit_events(key_label);
            CREATE INDEX IF NOT EXISTS idx_rl_model  ON rate_limit_events(model);
        ''')
        await self._db.commit()
        self._save_interval = asyncio.create_task(self._periodic_save())
        self._ready.set()

    async def ready(self):
        await self._ready.wait()

    async def _periodic_save(self):
        while True:
            await asyncio.sleep(30)
            await self._save()

    async def _save(self):
        if self._db:
            await self._db.commit()

    def _maybe_save(self):
        self._write_counter += 1
        if self._write_counter % 50 == 0:
            asyncio.create_task(self._save())

    async def record_request(self, **kwargs):
        await self._ready.wait()
        try:
            prompt_tokens = kwargs.get('promptTokens', 0) or 0
            completion_tokens = kwargs.get('completionTokens', 0) or 0
            total_tokens = kwargs.get('totalTokens') or (prompt_tokens + completion_tokens)
            ttft = kwargs.get('ttftMs', 0) or 0

            await self._db.execute(
                '''INSERT INTO requests
                   (ts, method, path, model, key_label, streaming, status_code,
                    latency_ms, prompt_tokens, completion_tokens, cached_tokens,
                    total_tokens, was_rate_limited, retries, request_bytes, pacing_ms, ttft_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [
                    time.time(), kwargs.get('method', 'POST'), kwargs.get('path', ''),
                    kwargs.get('model', ''), kwargs.get('keyLabel', ''),
                    1 if kwargs.get('streaming') else 0, kwargs.get('statusCode', 200),
                    kwargs.get('latencyMs', 0), prompt_tokens, completion_tokens,
                    kwargs.get('cachedTokens', 0), total_tokens,
                    1 if kwargs.get('wasRateLimited') else 0, kwargs.get('retries', 0),
                    kwargs.get('requestBytes', 0), kwargs.get('pacingMs', 0), ttft,
                ]
            )
            self._maybe_save()
            if self._on_request:
                self._on_request({
                    'ts': time.time(),
                    'method': kwargs.get('method', 'POST'),
                    'path': kwargs.get('path', ''),
                    'model': kwargs.get('model', ''),
                    'key_label': kwargs.get('keyLabel', ''),
                    'streaming': 1 if kwargs.get('streaming') else 0,
                    'status_code': kwargs.get('statusCode', 200),
                    'latency_ms': kwargs.get('latencyMs', 0),
                    'ttft_ms': ttft,
                    'prompt_tokens': prompt_tokens,
                    'completion_tokens': completion_tokens,
                    'cached_tokens': kwargs.get('cachedTokens', 0),
                    'total_tokens': total_tokens,
                    'was_rate_limited': 1 if kwargs.get('wasRateLimited') else 0,
                    'retries': kwargs.get('retries', 0),
                    'request_bytes': kwargs.get('requestBytes', 0),
                    'pacing_ms': kwargs.get('pacingMs', 0),
                })
        except Exception as e:
            logger.error(f'[metrics] recordRequest error: {e}')

    async def record_rate_limit_event(self, **kwargs):
        await self._ready.wait()
        try:
            await self._db.execute(
                '''INSERT INTO rate_limit_events
                   (ts, key_label, model, retry_after_s, detected_limit, rotated_to, scope, observed_rpm)
                   VALUES (?,?,?,?,?,?,?,?)''',
                [
                    time.time(), kwargs.get('keyLabel', ''), kwargs.get('model', ''),
                    kwargs.get('retryAfterS', 0), kwargs.get('detectedLimit'),
                    kwargs.get('rotatedTo'), kwargs.get('scope', 'key'),
                    kwargs.get('observedRpm'),
                ]
            )
            self._maybe_save()
            if self._on_rate_limit:
                self._on_rate_limit({
                    'ts': time.time(),
                    'key_label': kwargs.get('keyLabel', ''),
                    'model': kwargs.get('model', ''),
                    'retry_after_s': kwargs.get('retryAfterS', 0),
                    'detected_limit': kwargs.get('detectedLimit'),
                    'rotated_to': kwargs.get('rotatedTo'),
                    'scope': kwargs.get('scope', 'key'),
                    'observed_rpm': kwargs.get('observedRpm'),
                })
        except Exception as e:
            logger.error(f'[metrics] recordRateLimitEvent error: {e}')

    async def set_model_status(self, model, ok, last_status, reason, endpoint):
        await self._ready.wait()
        try:
            await self._db.execute(
                '''INSERT INTO model_status (model, ok, last_status, reason, endpoint, checked_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(model) DO UPDATE SET
                     ok=excluded.ok, last_status=excluded.last_status, reason=excluded.reason,
                     endpoint=excluded.endpoint, checked_at=excluded.checked_at''',
                [model, 1 if ok else 0, last_status, reason, endpoint, time.time()]
            )
            self._maybe_save()
        except Exception as e:
            logger.error(f'[metrics] setModelStatus error: {e}')

    async def get_model_status(self) -> dict:
        try:
            cursor = await self._db.execute("SELECT * FROM model_status")
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return {row[0]: dict(zip(cols, row, strict=False)) for row in rows}
        except Exception:
            return {}

    async def get_unavailable_models(self) -> set:
        try:
            cursor = await self._db.execute("SELECT model FROM model_status WHERE ok=0")
            rows = await cursor.fetchall()
            return {row[0] for row in rows}
        except Exception:
            return set()

    async def get_unavailable_models_detailed(self) -> list:
        try:
            cursor = await self._db.execute(
                "SELECT model, last_status, reason FROM model_status WHERE ok=0"
            )
            rows = await cursor.fetchall()
            return [{'model': r[0], 'last_status': r[1], 'reason': r[2]} for r in rows]
        except Exception:
            return []

    async def avg_latency_24h(self) -> int:
        try:
            since = time.time() - 86400
            cursor = await self._db.execute(
                "SELECT CAST(AVG(latency_ms) AS INTEGER) AS avg FROM requests WHERE ts >= ?",
                (since,)
            )
            row = await cursor.fetchone()
            return row[0] or 0
        except Exception:
            return 0

    async def exhaustion_count_24h(self) -> int:
        try:
            since = time.time() - 86400
            cursor = await self._db.execute(
                "SELECT COUNT(*) AS cnt FROM rate_limit_events WHERE ts >= ?",
                (since,)
            )
            row = await cursor.fetchone()
            return row[0] or 0
        except Exception:
            return 0

    async def recent_requests(self, limit=100, offset=0) -> list:
        try:
            cursor = await self._db.execute(
                "SELECT * FROM requests ORDER BY ts DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]
        except Exception:
            return []

    async def rate_limit_events(self, limit=100) -> list:
        try:
            cursor = await self._db.execute(
                "SELECT * FROM rate_limit_events ORDER BY ts DESC LIMIT ?",
                (limit,)
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]
        except Exception:
            return []

    async def rate_limit_summary(self, window_str='24h') -> dict:
        try:
            window_secs = WINDOW_SECS.get(window_str, 86400)
            since = time.time() - window_secs
            cursor = await self._db.execute(
                "SELECT COALESCE(scope,'key') AS scope, COUNT(*) AS n FROM rate_limit_events WHERE ts >= ? GROUP BY scope",
                (since,)
            )
            rows = await cursor.fetchall()
            result = {r[0]: r[1] for r in rows}
            key_events = result.get('key', 0)
            model_events = result.get('model', 0)
            return {'key_events': key_events, 'model_events': model_events, 'total': key_events + model_events}
        except Exception:
            return {'key_events': 0, 'model_events': 0, 'total': 0}

    async def prune(self, days=30):
        try:
            cutoff = time.time() - days * 86400
            await self._db.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            await self._db.execute("DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,))
            await self._db.execute("DELETE FROM model_status WHERE checked_at < ?", (cutoff,))
            await self._db.commit()
        except Exception:
            pass

    async def summary(self, window_str='24h') -> dict:
        try:
            window_secs = WINDOW_SECS.get(window_str, 86400)
            now = time.time()
            since = now - window_secs

            cursor = await self._db.execute('''
                SELECT COUNT(*)               AS total_requests,
                       SUM(prompt_tokens)     AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(cached_tokens)     AS cached_tokens,
                       SUM(total_tokens)      AS total_tokens,
                       AVG(latency_ms)        AS avg_latency_ms,
                       SUM(was_rate_limited)  AS rate_limited_count,
                       SUM(retries)           AS total_retries,
                       SUM(pacing_ms)         AS total_pacing_ms,
                       SUM(CASE WHEN pacing_ms > 0 THEN 1 ELSE 0 END) AS paced_requests,
                       SUM(streaming)         AS streaming_count,
                       AVG(CASE WHEN streaming = 1 AND ttft_ms > 0 THEN ttft_ms ELSE NULL END) AS avg_ttft_ms
                  FROM requests WHERE ts >= ?
            ''', (since,))
            r = await cursor.fetchone()

            total_requests = r[0] or 0
            total_tokens = r[4] or 0
            cached_tokens = r[3] or 0
            cache_pct = round(cached_tokens / total_tokens * 100, 1) if total_tokens else 0

            cursor = await self._db.execute(
                "SELECT COUNT(*) AS c FROM requests WHERE ts >= ? AND latency_ms > 0",
                (since,)
            )
            cnt_row = await cursor.fetchone()
            cnt = cnt_row[0] or 0

            async def get_pctl(p):
                if not cnt:
                    return 0
                off = min(cnt - 1, int(cnt * p))
                cursor = await self._db.execute(
                    "SELECT latency_ms FROM requests WHERE ts >= ? AND latency_ms > 0 ORDER BY latency_ms LIMIT 1 OFFSET ?",
                    (since, off)
                )
                row = await cursor.fetchone()
                return row[0] or 0

            p95 = await get_pctl(0.95)
            p99 = await get_pctl(0.99)

            async def get_req_count_since(secs):
                cursor = await self._db.execute(
                    "SELECT COUNT(*) AS c FROM requests WHERE ts >= ?",
                    (now - secs,)
                )
                row = await cursor.fetchone()
                return row[0] or 0

            req_1m = await get_req_count_since(60)
            req_5m = await get_req_count_since(300)
            req_1h = await get_req_count_since(3600)
            req_24h = await get_req_count_since(86400)

            return {
                'window': window_str,
                'total_requests': total_requests,
                'prompt_tokens': r[1] or 0,
                'completion_tokens': r[2] or 0,
                'cached_tokens': cached_tokens,
                'total_tokens': total_tokens,
                'cache_hit_pct': cache_pct,
                'avg_latency_ms': round(r[5], 1) if r[5] else 0.0,
                'p95_latency_ms': round(p95, 1),
                'p99_latency_ms': round(p99, 1),
                'rate_limited_count': r[6] or 0,
                'total_retries': r[7] or 0,
                'total_pacing_ms': round(r[8]) if r[8] else 0,
                'paced_requests': r[9] or 0,
                'streaming_count': r[10] or 0,
                'avg_ttft_ms': round(r[11], 1) if r[11] else 0.0,
                'req_per_min': req_1m,
                'req_per_5min': req_5m,
                'req_per_hour': req_1h,
                'req_per_day': req_24h,
            }
        except Exception as e:
            logger.error(f'metrics.summary error: {e}')
            return {}

    async def get_per_model(self, window_str='24h') -> list:
        try:
            window_secs = WINDOW_SECS.get(window_str, 86400)
            since = time.time() - window_secs
            cursor = await self._db.execute('''
                SELECT model,
                       COUNT(*)                AS requests,
                       SUM(total_tokens)       AS total_tokens,
                       SUM(prompt_tokens)      AS prompt_tokens,
                       SUM(completion_tokens)  AS completion_tokens,
                       SUM(cached_tokens)      AS cached_tokens,
                       ROUND(AVG(latency_ms),1) AS avg_latency_ms,
                       ROUND(MAX(latency_ms),1) AS max_latency_ms,
                       SUM(was_rate_limited)   AS rate_limited,
                       SUM(retries)            AS total_retries,
                       SUM(CASE WHEN status_code <  400 THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
                       SUM(streaming)          AS streaming_count,
                       COUNT(DISTINCT key_label) AS keys_used,
                       MAX(ts)                 AS last_ts,
                       MIN(ts)                 AS first_ts,
                       ROUND(AVG(CASE WHEN streaming = 1 AND ttft_ms > 0 THEN ttft_ms ELSE NULL END), 1) AS avg_ttft_ms
                  FROM requests WHERE ts >= ?
                 GROUP BY model ORDER BY requests DESC
            ''', (since,))
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            out = []
            for row in rows:
                d = dict(zip(cols, row, strict=False))
                req = d.get('requests', 0)
                d['non_streaming_count'] = req - (d.get('streaming_count', 0) or 0)
                d['success_rate'] = round((d.get('success_count', 0) or 0) / req * 100, 1) if req else 0.0
                d['avg_total_tokens'] = round((d.get('total_tokens', 0) or 0) / req) if req else 0
                d['avg_output_tokens'] = round((d.get('completion_tokens', 0) or 0) / req) if req else 0
                tt = d.get('total_tokens', 0) or 0
                d['cache_hit_pct'] = round((d.get('cached_tokens', 0) or 0) / tt * 100, 1) if tt else 0.0
                d['avg_ttft_ms'] = d.get('avg_ttft_ms') or 0.0
                out.append(d)
            return out
        except Exception:
            return []

    async def get_model_timeseries(self, model: str, hours: int = 24) -> list:
        try:
            now = time.time()
            out = []
            for i in range(hours, 0, -1):
                start = now - i * 3600
                end = now - (i - 1) * 3600
                cursor = await self._db.execute(
                    "SELECT COUNT(*) AS req, COALESCE(SUM(total_tokens), 0) AS tok, COALESCE(SUM(was_rate_limited), 0) AS rl FROM requests WHERE model = ? AND ts >= ? AND ts < ?",
                    (model, start, end)
                )
                row = await cursor.fetchone()
                out.append({
                    'hour_ago': i,
                    'requests': row[0] or 0,
                    'total_tokens': row[1] or 0,
                    'rate_limited': row[2] or 0,
                })
            return out
        except Exception:
            return []

    async def get_per_key(self, window_str='24h') -> list:
        try:
            window_secs = WINDOW_SECS.get(window_str, 86400)
            since = time.time() - window_secs
            cursor = await self._db.execute('''
                SELECT key_label,
                       COUNT(*)                 AS requests,
                       SUM(total_tokens)        AS total_tokens,
                       SUM(prompt_tokens)       AS prompt_tokens,
                       SUM(completion_tokens)   AS completion_tokens,
                       SUM(cached_tokens)       AS cached_tokens,
                       ROUND(AVG(latency_ms),1) AS avg_latency_ms,
                       SUM(was_rate_limited)    AS rate_limited_count,
                       SUM(retries)             AS total_retries
                  FROM requests
                  WHERE ts >= ? AND key_label NOT IN ('none', 'exhausted')
                  GROUP BY key_label ORDER BY requests DESC
            ''', (since,))
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]
        except Exception:
            return []

    async def _bucket_chart(self, bucket_secs: int, n: int) -> list:
        try:
            now = time.time()
            out = []
            for i in range(n, 0, -1):
                start = now - i * bucket_secs
                end = now - (i - 1) * bucket_secs
                cursor = await self._db.execute(
                    "SELECT COUNT(*) AS req, COALESCE(SUM(total_tokens), 0) AS tok, COALESCE(SUM(was_rate_limited), 0) AS rl FROM requests WHERE ts >= ? AND ts < ?",
                    (start, end)
                )
                row = await cursor.fetchone()
                out.append({
                    'index': n - i,
                    'requests': row[0] or 0,
                    'total_tokens': row[1] or 0,
                    'rate_limited': row[2] or 0,
                })
            return out
        except Exception:
            return []

    async def get_hourly_chart(self, hours=24) -> list:
        data = await self._bucket_chart(3600, hours)
        for d in data:
            d['hour_ago'] = hours - d['index']
            del d['index']
        return data

    async def get_daily_chart(self, days=30) -> list:
        data = await self._bucket_chart(86400, days)
        for d in data:
            d['day_ago'] = days - d['index']
            del d['index']
        return data

    async def get_total_counts(self) -> dict:
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) AS total_requests, COALESCE(SUM(total_tokens), 0) AS total_tokens, COALESCE(SUM(was_rate_limited), 0) AS total_rl_requests FROM requests"
            )
            row = await cursor.fetchone()

            cursor2 = await self._db.execute("SELECT COUNT(*) AS c FROM rate_limit_events")
            row2 = await cursor2.fetchone()

            return {
                'all_time_requests': row[0] or 0,
                'all_time_tokens': row[1] or 0,
                'all_time_rl_requests': row[2] or 0,
                'all_time_rl_events': row2[0] or 0,
            }
        except Exception:
            return {'all_time_requests': 0, 'all_time_tokens': 0, 'all_time_rl_requests': 0, 'all_time_rl_events': 0}

    async def reset_all(self) -> dict:
        try:
            cursor = await self._db.execute("SELECT COUNT(*) AS c FROM requests")
            row = await cursor.fetchone()
            n_req = row[0] or 0

            cursor = await self._db.execute("SELECT COUNT(*) AS c FROM rate_limit_events")
            row = await cursor.fetchone()
            n_rl = row[0] or 0

            await self._db.execute("DELETE FROM requests")
            await self._db.execute("DELETE FROM rate_limit_events")
            await self._db.commit()
            logger.info(f'[metrics] Reset: removed {n_req} requests, {n_rl} rate-limit events')
            return {'requests_removed': n_req, 'rate_limit_events_removed': n_rl}
        except Exception as e:
            logger.error(f'metrics.resetAll error: {e}')
            return {'requests_removed': 0, 'rate_limit_events_removed': 0}

    async def close(self):
        try:
            if self._save_interval:
                self._save_interval.cancel()
            await self._save()
            if self._db:
                await self._db.close()
                self._db = None
        except Exception:
            pass
