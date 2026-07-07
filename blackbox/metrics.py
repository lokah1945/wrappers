"""
metrics.py v1 — Persistent SQLite metrics store for wrapper-blackbox.
Tracks every request: tokens, latency, key used, rate limit events.

Thread-safe via per-thread connections (works correctly when called through
asyncio.to_thread from the async layer). WAL + busy_timeout handle concurrent
readers and serialized writers at the database level — no Python-level global
lock, so a slow dashboard read never blocks a metrics write.

Adapted from wrapper-nvidia metrics.py for Blackbox AI.
"""
import sqlite3, time, threading, logging
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "metrics.db"
log = logging.getLogger("metrics")

_local = threading.local()

_SCHEMA = """
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
    request_bytes     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    key_label      TEXT,
    model          TEXT,
    retry_after_s  INTEGER,
    detected_limit INTEGER,
    rotated_to     TEXT,
    scope          TEXT DEFAULT 'key'
);
CREATE INDEX IF NOT EXISTS idx_req_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_req_key   ON requests(key_label);
CREATE INDEX IF NOT EXISTS idx_rl_ts     ON rate_limit_events(ts);
"""


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        # Migration: add scope column to pre-existing rate_limit_events tables
        try:
            conn.execute("ALTER TABLE rate_limit_events ADD COLUMN scope TEXT DEFAULT 'key'")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
        _local.conn = conn
    return conn


# ── Writers ────────────────────────────────────────────────────────────

def record_request(
    method: str, path: str, model: str, key_label: str, streaming: bool,
    status_code: int, latency_ms: float,
    prompt_tokens: int = 0, completion_tokens: int = 0,
    cached_tokens: int = 0, total_tokens: int = 0,
    was_rate_limited: bool = False, retries: int = 0,
    request_bytes: int = 0,
):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO requests
           (ts, method, path, model, key_label, streaming, status_code,
            latency_ms, prompt_tokens, completion_tokens, cached_tokens,
            total_tokens, was_rate_limited, retries, request_bytes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), method, path, model, key_label, int(streaming),
         status_code, latency_ms, prompt_tokens, completion_tokens,
         cached_tokens, total_tokens, int(was_rate_limited), retries, request_bytes),
    )
    conn.commit()


def record_rate_limit_event(
    key_label: str, model: str, retry_after_s: int,
    detected_limit: Optional[int], rotated_to: Optional[str],
    scope: str = "key",
):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO rate_limit_events
           (ts, key_label, model, retry_after_s, detected_limit, rotated_to, scope)
           VALUES (?,?,?,?,?,?,?)""",
        (time.time(), key_label, model, retry_after_s, detected_limit, rotated_to, scope),
    )
    conn.commit()


# ── Time window helper ─────────────────────────────────────────────────

_WINDOWS = {"1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}

def _since(window: str) -> float:
    return time.time() - _WINDOWS.get(window, 86400)


# ── Readers ────────────────────────────────────────────────────────────

def get_summary(window: str = "24h") -> dict:
    conn = _get_conn()
    since = _since(window)
    now = time.time()

    r = conn.execute("""
        SELECT COUNT(*)               AS total_requests,
               SUM(prompt_tokens)     AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(cached_tokens)     AS cached_tokens,
               SUM(total_tokens)      AS total_tokens,
               AVG(latency_ms)        AS avg_latency_ms,
               SUM(was_rate_limited)  AS rate_limited_count,
               SUM(retries)           AS total_retries
        FROM requests WHERE ts >= ?
    """, (since,)).fetchone()

    # Percentiles via SQL OFFSET (no full row materialization in Python)
    cnt = conn.execute(
        "SELECT COUNT(*) FROM requests WHERE ts >= ? AND latency_ms > 0", (since,)
    ).fetchone()[0]

    def _pctl(p):
        if not cnt:
            return 0
        off = min(cnt - 1, int(cnt * p))
        row = conn.execute(
            "SELECT latency_ms FROM requests WHERE ts >= ? AND latency_ms > 0 "
            "ORDER BY latency_ms LIMIT 1 OFFSET ?", (since, off)
        ).fetchone()
        return row[0] if row else 0

    p95 = _pctl(0.95)
    p99 = _pctl(0.99)

    req_1m  = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (now - 60,)).fetchone()[0]
    req_5m  = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (now - 300,)).fetchone()[0]
    req_1h  = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (now - 3600,)).fetchone()[0]
    req_24h = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (now - 86400,)).fetchone()[0]

    total_tok = r["total_tokens"] or 0
    cached    = r["cached_tokens"] or 0
    cache_pct = round(cached / total_tok * 100, 1) if total_tok else 0.0

    return {
        "window": window,
        "total_requests": r["total_requests"] or 0,
        "prompt_tokens": r["prompt_tokens"] or 0,
        "completion_tokens": r["completion_tokens"] or 0,
        "cached_tokens": r["cached_tokens"] or 0,
        "total_tokens": total_tok,
        "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
        "p95_latency_ms": round(p95, 1),
        "p99_latency_ms": round(p99, 1),
        "rate_limited_count": r["rate_limited_count"] or 0,
        "total_retries": r["total_retries"] or 0,
        "cache_hit_pct": cache_pct,
        "rpm_1m": req_1m,
        "rpm_5m": req_5m,
        "rpm_1h": req_1h,
        "rpm_24h": req_24h,
    }


def get_model_breakdown(window: str = "24h") -> list:
    conn = _get_conn()
    since = _since(window)
    rows = conn.execute("""
        SELECT model,
               COUNT(*)                 AS requests,
               SUM(total_tokens)        AS total_tokens,
               SUM(prompt_tokens)       AS prompt_tokens,
               SUM(completion_tokens)   AS completion_tokens,
               SUM(cached_tokens)       AS cached_tokens,
               ROUND(AVG(latency_ms),1) AS avg_latency_ms,
               SUM(was_rate_limited)    AS rate_limited_count,
               SUM(retries)             AS total_retries
        FROM requests
        WHERE ts >= ?
        GROUP BY model ORDER BY requests DESC
    """, (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        req = d["requests"]
        d["success_rate_pct"] = round((req - (d["rate_limited_count"] or 0)) / req * 100, 1) if req else 0.0
        d["avg_total_tokens"] = round((d["total_tokens"] or 0) / req, 0) if req else 0
        d["avg_output_tokens"] = round((d["completion_tokens"] or 0) / req, 0) if req else 0
        tt = d["total_tokens"] or 0
        d["cache_hit_pct"] = round((d["cached_tokens"] or 0) / tt * 100, 1) if tt else 0.0
        out.append(d)
    return out


def get_model_timeseries(model: str, hours: int = 24) -> list:
    """Hourly request/token buckets for ONE model (dashboard drill-down)."""
    conn = _get_conn()
    now = time.time()
    out = []
    for i in range(hours, 0, -1):
        start = now - i * 3600
        end   = now - (i - 1) * 3600
        row = conn.execute("""
            SELECT COUNT(*) AS req,
                   COALESCE(SUM(total_tokens), 0) AS tok,
                   COALESCE(SUM(was_rate_limited), 0) AS rl
            FROM requests WHERE model = ? AND ts >= ? AND ts < ?
        """, (model, start, end)).fetchone()
        out.append({"hour_ago": i, "requests": row[0],
                    "total_tokens": row[1], "rate_limited": row[2]})
    return out


def get_per_key(window: str = "24h") -> list:
    conn = _get_conn()
    rows = conn.execute("""
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
    """, (_since(window),)).fetchall()
    return [dict(r) for r in rows]


def get_activity_log(limit: int = 100, offset: int = 0) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM requests ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [dict(r) for r in rows]


def get_rate_limit_events(limit: int = 100) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM rate_limit_events ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_rate_limit_summary(window: str = "24h") -> dict:
    """Count rate-limit events split by scope (key vs model) in the window."""
    conn = _get_conn()
    since = _since(window)
    rows = conn.execute(
        "SELECT COALESCE(scope,'key') AS scope, COUNT(*) AS n "
        "FROM rate_limit_events WHERE ts >= ? GROUP BY scope", (since,)
    ).fetchall()
    by = {r["scope"]: r["n"] for r in rows}
    return {"key_events": by.get("key", 0), "model_events": by.get("model", 0),
            "total": sum(by.values())}


def _bucket_chart(bucket_secs: int, n: int) -> list:
    conn = _get_conn()
    now = time.time()
    out = []
    for i in range(n, 0, -1):
        start = now - i * bucket_secs
        end   = now - (i - 1) * bucket_secs
        row = conn.execute("""
            SELECT COUNT(*) AS req,
                   COALESCE(SUM(total_tokens), 0) AS tok,
                   COALESCE(SUM(was_rate_limited), 0) AS rl
            FROM requests WHERE ts >= ? AND ts < ?
        """, (start, end)).fetchone()
        out.append({"index": n - i, "requests": row[0],
                    "total_tokens": row[1], "rate_limited": row[2]})
    return out


def get_hourly_chart(hours: int = 24) -> list:
    data = _bucket_chart(3600, hours)
    for d in data:
        d["hour_ago"] = hours - d.pop("index")
    return data


def get_daily_chart(days: int = 30) -> list:
    data = _bucket_chart(86400, days)
    for d in data:
        d["day_ago"] = days - d.pop("index")
    return data


def get_total_counts() -> dict:
    conn = _get_conn()
    r = conn.execute("""
        SELECT COUNT(*) AS total_requests,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(was_rate_limited), 0) AS total_rl_requests
        FROM requests
    """).fetchone()
    rl = conn.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]
    return {
        "all_time_requests":     r["total_requests"],
        "all_time_tokens":       r["total_tokens"],
        "all_time_rl_requests":  r["total_rl_requests"],
        "all_time_rl_events":    rl,
    }


def reset_all() -> dict:
    """Wipe all recorded metrics (requests + rate-limit events). Returns counts removed."""
    conn = _get_conn()
    n_req = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    n_rl  = conn.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]
    conn.execute("DELETE FROM requests")
    conn.execute("DELETE FROM rate_limit_events")
    conn.commit()
    try:
        conn.execute("VACUUM")
    except Exception:
        pass
    log.info("Metrics reset: removed %d requests, %d rate-limit events", n_req, n_rl)
    return {"requests_removed": n_req, "rate_limit_events_removed": n_rl}


def prune_old_data(days: int = 30):
    cutoff = time.time() - days * 86400
    conn = _get_conn()
    n = conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,))
    conn.commit()
    if n:
        log.info("Pruned %d records older than %dd", n, days)


# Ensure schema exists at import (main-thread connection)
_get_conn()