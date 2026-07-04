"""
metrics.py — Persistent SQLite metrics store for wrapper-codex.

Tracks every CLI run: latency, exit code, status, model, cwd, output size,
errors. Plus a per-event stream for replay.

Schema is identical to wrapper-claude-code so the dashboards can share HTML
templates if you symlink one.

Thread-safe via per-thread connections (works correctly when called through
asyncio.to_thread from the async layer). WAL + busy_timeout handle concurrent
readers and serialized writers at the database level.
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "metrics.db"
_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT,
    provider TEXT,
    model TEXT,
    cwd TEXT,
    status TEXT,
    exit_code INTEGER,
    latency_ms REAL DEFAULT 0,
    output_chars INTEGER DEFAULT 0,
    stderr_chars INTEGER DEFAULT 0,
    final_text_chars INTEGER DEFAULT 0,
    request_bytes INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT,
    event TEXT,
    stream TEXT,
    size INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_ts      ON runs(ts);
CREATE INDEX IF NOT EXISTS idx_runs_run_id  ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_model   ON runs(model);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
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
        conn.commit()
        _local.conn = conn
    return conn


# ── Writers ──────────────────────────────────────────────────────────────

def record_run(run_id: str, provider: str, model: str, cwd: str, status: str,
               exit_code: Optional[int], latency_ms: float, output_chars: int,
               stderr_chars: int, request_bytes: int, final_text_chars: int = 0,
               error: str = ""):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO runs
           (ts, run_id, provider, model, cwd, status, exit_code, latency_ms,
            output_chars, stderr_chars, final_text_chars, request_bytes, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), run_id, provider, model, cwd, status, exit_code, latency_ms,
         output_chars, stderr_chars, final_text_chars, request_bytes, error),
    )
    conn.commit()


def record_event(run_id: str, event: str, stream: str = "", size: int = 0):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO events (ts, run_id, event, stream, size) VALUES (?,?,?,?,?)",
        (time.time(), run_id, event, stream, size),
    )
    conn.commit()


# ── Time window helper ──────────────────────────────────────────────────

_WINDOWS = {"1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}


def _since(window: str) -> float:
    return time.time() - _WINDOWS.get(window, 86400)


# ── Readers ─────────────────────────────────────────────────────────────

def get_summary(window: str = "24h") -> dict:
    conn = _get_conn()
    since = _since(window)
    now = time.time()
    row = conn.execute("""
        SELECT COUNT(*) AS total_runs,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
               ROUND(MIN(latency_ms), 1) AS min_latency_ms,
               ROUND(MAX(latency_ms), 1) AS max_latency_ms,
               SUM(output_chars)      AS output_chars,
               SUM(stderr_chars)      AS stderr_chars,
               SUM(final_text_chars)  AS final_text_chars,
               SUM(request_bytes)     AS request_bytes,
               SUM(CASE WHEN exit_code = 0 AND status='completed' THEN 1 ELSE 0 END) AS success_exit_zero,
               COUNT(DISTINCT model)  AS distinct_models,
               COUNT(DISTINCT cwd)    AS distinct_cwds
        FROM runs WHERE ts >= ?
    """, (since,)).fetchone()

    req_1m  = conn.execute("SELECT COUNT(*) FROM runs WHERE ts >= ?", (now - 60,)).fetchone()[0]
    req_5m  = conn.execute("SELECT COUNT(*) FROM runs WHERE ts >= ?", (now - 300,)).fetchone()[0]
    req_1h  = conn.execute("SELECT COUNT(*) FROM runs WHERE ts >= ?", (now - 3600,)).fetchone()[0]
    req_24h = conn.execute("SELECT COUNT(*) FROM runs WHERE ts >= ?", (now - 86400,)).fetchone()[0]

    total = row["total_runs"] or 0
    completed = row["completed"] or 0
    success_rate = round(completed / total * 100, 1) if total else 0.0

    return {
        "window": window,
        "total_runs": total,
        "completed": completed,
        "failed": row["failed"] or 0,
        "cancelled": row["cancelled"] or 0,
        "success_rate": success_rate,
        "avg_latency_ms": row["avg_latency_ms"] or 0,
        "min_latency_ms": row["min_latency_ms"] or 0,
        "max_latency_ms": row["max_latency_ms"] or 0,
        "output_chars": row["output_chars"] or 0,
        "stderr_chars": row["stderr_chars"] or 0,
        "final_text_chars": row["final_text_chars"] or 0,
        "request_bytes": row["request_bytes"] or 0,
        "distinct_models": row["distinct_models"] or 0,
        "distinct_cwds": row["distinct_cwds"] or 0,
        "req_per_min": req_1m,
        "req_per_5min": req_5m,
        "req_per_hour": req_1h,
        "req_per_day": req_24h,
    }


def get_per_model(window: str = "24h") -> list:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT model,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
               ROUND(MAX(latency_ms), 1) AS max_latency_ms,
               SUM(output_chars)     AS output_chars,
               SUM(final_text_chars) AS final_text_chars
        FROM runs WHERE ts >= ? AND model IS NOT NULL AND model != ''
        GROUP BY model ORDER BY runs DESC
    """, (_since(window),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        runs = d["runs"] or 0
        d["success_rate"] = round((d["completed"] or 0) / runs * 100, 1) if runs else 0.0
        d["avg_output_chars"] = round((d["output_chars"] or 0) / runs, 0) if runs else 0
        out.append(d)
    return out


def get_recent_runs(limit: int = 50) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_activity_log(limit: int = 100, offset: int = 0) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [dict(r) for r in rows]


def _bucket_chart(bucket_secs: int, n: int) -> list:
    conn = _get_conn()
    now = time.time()
    out = []
    for i in range(n, 0, -1):
        start = now - i * bucket_secs
        end = now - (i - 1) * bucket_secs
        row = conn.execute("""
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) AS completed,
                   COALESCE(SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END), 0) AS failed,
                   COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END), 0) AS cancelled
            FROM runs WHERE ts >= ? AND ts < ?
        """, (start, end)).fetchone()
        out.append({"index": n - i, "runs": row[0],
                    "completed": row[1], "failed": row[2], "cancelled": row[3]})
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
        SELECT COUNT(*) AS total_runs,
               COALESCE(SUM(final_text_chars), 0) AS total_final_chars
        FROM runs
    """).fetchone()
    ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {
        "all_time_runs": r["total_runs"] or 0,
        "all_time_final_chars": r["total_final_chars"] or 0,
        "all_time_events": ev,
    }


def reset_all() -> dict:
    """Wipe all recorded runs + events. Returns counts removed."""
    conn = _get_conn()
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    n_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM events")
    conn.commit()
    try:
        conn.execute("VACUUM")
    except Exception:
        pass
    return {"runs_removed": n_runs, "events_removed": n_ev}


def prune_old_data(days: int = 30):
    cutoff = time.time() - days * 86400
    conn = _get_conn()
    n = conn.execute("DELETE FROM runs WHERE ts < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.commit()
    if n:
        import logging
        logging.getLogger("metrics").info("Pruned %d runs older than %dd", n, days)


# Approximate token estimate (~4 chars per token). Cheap heuristic for budgeting.
def estimate_tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))


# Ensure schema exists at import time.
_get_conn()
