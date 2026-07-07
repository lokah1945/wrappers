/**
 * metrics.js — SQLite-backed request metrics (using sql.js — pure JS, no native build)
 * Ported from Python metrics.py — functionally identical.
 *
 * Tables: requests, model_status, rate_limit_events
 */

const initSqlJs = require('sql.js');
const fs = require('fs');
const path = require('path');

class Metrics {
  constructor(dbPath) {
    this._dbPath = dbPath;
    this._db = null;
    this._writeCounter = 0;
    this._ready = this._init();
    this._onRequest = null;
    this._onRateLimit = null;
  }

  onRequest(cb) { this._onRequest = cb; }
  onRateLimit(cb) { this._onRateLimit = cb; }

  async _init() {
    const SQL = await initSqlJs();

    // Load existing DB if present
    if (fs.existsSync(this._dbPath)) {
      const buf = fs.readFileSync(this._dbPath);
      this._db = new SQL.Database(buf);
    } else {
      this._db = new SQL.Database();
    }

    // Check if the database has the old Node.js schema (missing total_tokens or ttft_ms)
    // Migrate instead of reset to preserve historical data
    try {
      const tableExists = this._withStmt("SELECT name FROM sqlite_master WHERE type='table' AND name='requests'", (s) => {
        return s.step();
      });

      if (tableExists) {
        let hasTotalTokens = false;
        let hasTtftMs = false;
        this._withStmt("PRAGMA table_info(requests)", (stmt) => {
          while (stmt.step()) {
            const col = stmt.getAsObject().name;
            if (col === 'total_tokens') hasTotalTokens = true;
            if (col === 'ttft_ms') hasTtftMs = true;
          }
        });

        if (!hasTotalTokens) {
          console.log("[metrics] Migrating schema: adding total_tokens column...");
          this._db.run("ALTER TABLE requests ADD COLUMN total_tokens INTEGER DEFAULT 0");
        }
        if (!hasTtftMs) {
          console.log("[metrics] Migrating schema: adding ttft_ms column...");
          this._db.run("ALTER TABLE requests ADD COLUMN ttft_ms REAL DEFAULT 0");
        }
      }
    } catch (e) {
      console.warn("[metrics] Schema check failed:", e.message);
    }

    this._db.run(`
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
    `);

    this._save();
    // Periodic save every 30s
    this._saveInterval = setInterval(() => this._save(), 30000);
  }

  async ready() { await this._ready; }

  /**
   * Helper: prepare a statement, run callback, guarantee free() via try/finally.
   * Prevents statement leaks when bind/step/getAsObject throw.
   */
  _withStmt(sql, fn) {
    const stmt = this._db.prepare(sql);
    try {
      return fn(stmt);
    } finally {
      stmt.free();
    }
  }

  _save(sync = false) {
    if (sync) {
      try {
        if (!this._db) return;
        const data = this._db.export();
        const buffer = Buffer.from(data);
        fs.writeFileSync(this._dbPath + '.tmp', buffer);
        fs.renameSync(this._dbPath + '.tmp', this._dbPath);
      } catch (e) {
        console.error("[metrics] Synchronous save error:", e ? e.message || e : "unknown");
      }
      return;
    }

    if (this._savePending) return;
    this._savePending = true;
    setImmediate(async () => {
      try {
        if (!this._db) return;
        const data = this._db.export();
        const buffer = Buffer.from(data);
        await fs.promises.writeFile(this._dbPath + '.tmp', buffer);
        await fs.promises.rename(this._dbPath + '.tmp', this._dbPath);
      } catch (e) {
        console.error("[metrics] Save error:", e ? e.message || e : "unknown");
      } finally {
        this._savePending = false;
      }
    });
  }

  _maybeSave() {
    this._writeCounter = (this._writeCounter || 0) + 1;
    if (this._writeCounter % 50 === 0) {
      this._save();
    }
  }

  /** Record a completed request. */
  async recordRequest({
    method, path, model, keyLabel, streaming, statusCode, latencyMs,
    promptTokens, completionTokens, cachedTokens, totalTokens,
    wasRateLimited, retries, requestBytes, pacingMs, ttftMs
  }) {
    await this._ready;
    try {
      // Compute total_tokens if not provided
      const computedTotalTokens = totalTokens ?? ((promptTokens ?? 0) + (completionTokens ?? 0));
      const ttf = ttftMs || 0;

      this._db.run(
        `INSERT INTO requests
           (ts, method, path, model, key_label, streaming, status_code,
            latency_ms, prompt_tokens, completion_tokens, cached_tokens,
            total_tokens, was_rate_limited, retries, request_bytes, pacing_ms, ttft_ms)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
        [
          Date.now() / 1000, method || 'POST', path || '', model || '', keyLabel || '',
          streaming ? 1 : 0, statusCode || 200, latencyMs || 0,
          promptTokens || 0, completionTokens || 0, cachedTokens || 0, computedTotalTokens,
          wasRateLimited ? 1 : 0, retries || 0, requestBytes || 0, pacingMs || 0, ttf
        ]
      );
      this._maybeSave();
      if (this._onRequest) {
        this._onRequest({
          ts: Date.now() / 1000,
          method: method || 'POST',
          path: path || '',
          model: model || '',
          key_label: keyLabel || '',
          streaming: streaming ? 1 : 0,
          status_code: statusCode || 200,
          latency_ms: latencyMs || 0,
          ttft_ms: ttf,
          prompt_tokens: promptTokens || 0,
          completion_tokens: completionTokens || 0,
          cached_tokens: cachedTokens || 0,
          total_tokens: computedTotalTokens,
          was_rate_limited: wasRateLimited ? 1 : 0,
          retries: retries || 0,
          request_bytes: requestBytes || 0,
          pacing_ms: pacingMs || 0,
        });
      }
    } catch (e) {
      console.error("[metrics] recordRequest error:", e.message);
    }
  }

  /** Record a rate-limit event. */
  async recordRateLimitEvent({
    keyLabel, model, retryAfterS, detectedLimit, rotatedTo, scope, observedRpm
  }) {
    await this._ready;
    try {
      this._db.run(
        `INSERT INTO rate_limit_events
           (ts, key_label, model, retry_after_s, detected_limit, rotated_to, scope, observed_rpm)
         VALUES (?,?,?,?,?,?,?,?)`,
        [
          Date.now() / 1000, keyLabel || '', model || '', retryAfterS || 0,
          detectedLimit || null, rotatedTo || null, scope || 'key', observedRpm || null
        ]
      );
      this._maybeSave();
      if (this._onRateLimit) {
        this._onRateLimit({
          ts: Date.now() / 1000,
          key_label: keyLabel || '',
          model: model || '',
          retry_after_s: retryAfterS || 0,
          detected_limit: detectedLimit || null,
          rotated_to: rotatedTo || null,
          scope: scope || 'key',
          observed_rpm: observedRpm || null,
        });
      }
    } catch (e) {
      console.error("[metrics] recordRateLimitEvent error:", e.message);
    }
  }

  /** Record model verification status. */
  async setModelStatus(model, ok, lastStatus, reason, endpoint) {
    await this._ready;
    try {
      this._db.run(
        `INSERT INTO model_status (model, ok, last_status, reason, endpoint, checked_at)
         VALUES (?,?,?,?,?,?)
         ON CONFLICT(model) DO UPDATE SET
           ok=excluded.ok, last_status=excluded.last_status, reason=excluded.reason,
           endpoint=excluded.endpoint, checked_at=excluded.checked_at`,
        [model, ok ? 1 : 0, lastStatus, reason, endpoint, Date.now() / 1000]
      );
      this._maybeSave();
    } catch (e) {
      console.error("[metrics] setModelStatus error:", e.message);
    }
  }

  getModelStatus() {
    try {
      return this._withStmt("SELECT * FROM model_status", (stmt) => {
        const out = {};
        while (stmt.step()) {
          const r = stmt.getAsObject();
          out[r.model] = r;
        }
        return out;
      });
    } catch { return {}; }
  }

  getUnavailableModels() {
    try {
      return this._withStmt("SELECT model FROM model_status WHERE ok=0", (stmt) => {
        const out = new Set();
        while (stmt.step()) {
          out.add(stmt.getAsObject().model);
        }
        return out;
      });
    } catch { return new Set(); }
  }

  /** Average latency for last 24h. */
  avgLatency24h() {
    try {
      const since = (Date.now() / 1000) - 86400;
      return this._withStmt(`SELECT CAST(AVG(latency_ms) AS INTEGER) AS avg FROM requests WHERE ts >= ?`, (stmt) => {
        stmt.bind([since]);
        stmt.step();
        return stmt.getAsObject().avg || 0;
      });
    } catch { return 0; }
  }

  /** Exhaustion events in last 24h. */
  exhaustionCount24h() {
    try {
      const since = (Date.now() / 1000) - 86400;
      return this._withStmt(`SELECT COUNT(*) AS cnt FROM rate_limit_events WHERE ts >= ?`, (stmt) => {
        stmt.bind([since]);
        stmt.step();
        return stmt.getAsObject().cnt || 0;
      });
    } catch { return 0; }
  }

  /** Recent requests for dashboard activity. */
  recentRequests(limit = 100, offset = 0) {
    try {
      return this._withStmt(`SELECT * FROM requests ORDER BY ts DESC LIMIT ? OFFSET ?`, (stmt) => {
        stmt.bind([limit, offset]);
        const out = [];
        while (stmt.step()) {
          out.push(stmt.getAsObject());
        }
        return out;
      });
    } catch { return []; }
  }

  /** Rate limit events log. */
  rateLimitEvents(limit = 100) {
    try {
      return this._withStmt(`SELECT * FROM rate_limit_events ORDER BY ts DESC LIMIT ?`, (stmt) => {
        stmt.bind([limit]);
        const out = [];
        while (stmt.step()) {
          out.push(stmt.getAsObject());
        }
        return out;
      });
    } catch { return []; }
  }

  /** Rate limit events summary. */
  rateLimitSummary(windowStr = "24h") {
    try {
      const windowSecs = { "1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 }[windowStr] || 86400;
      const since = (Date.now() / 1000) - windowSecs;
      const by = this._withStmt(`
        SELECT COALESCE(scope,'key') AS scope, COUNT(*) AS n
        FROM rate_limit_events WHERE ts >= ? GROUP BY scope
      `, (stmt) => {
        stmt.bind([since]);
        const result = {};
        while (stmt.step()) {
          const r = stmt.getAsObject();
          result[r.scope] = r.n;
        }
        return result;
      });
      const key_events = by.key || 0;
      const model_events = by.model || 0;
      const total = key_events + model_events;
      return { key_events, model_events, total };
    } catch { return { key_events: 0, model_events: 0, total: 0 }; }
  }

  /** Prune old data. */
  prune(days = 30) {
    try {
      const cutoff = (Date.now() / 1000) - days * 86400;
      this._db?.run(`DELETE FROM requests WHERE ts < ?`, [cutoff]);
      this._db?.run(`DELETE FROM rate_limit_events WHERE ts < ?`, [cutoff]);
      this._db?.run(`DELETE FROM model_status WHERE checked_at < ?`, [cutoff]);
      this._save();
    } catch {}
  }

  /** Summary stats. */
  summary(windowStr = "24h") {
    try {
      const windowSecs = { "1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 }[windowStr] || 86400;
      const now = Date.now() / 1000;
      const since = now - windowSecs;

      const r = this._withStmt(`
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
       `, (s1) => {
        s1.bind([since]);
        s1.step();
        return s1.getAsObject();
      });

      const total_requests = r.total_requests || 0;
      const total_tokens = r.total_tokens || 0;
      const cached_tokens = r.cached_tokens || 0;
      const cache_pct = total_tokens ? parseFloat((cached_tokens / total_tokens * 100).toFixed(1)) : 0;

      // Percentiles - using a more efficient approach
      const cnt = this._withStmt(`SELECT COUNT(*) AS c FROM requests WHERE ts >= ? AND latency_ms > 0`, (s2) => {
        s2.bind([since]);
        s2.step();
        return s2.getAsObject().c || 0;
      });

      const getPctl = (p) => {
        if (!cnt) return 0;
        const off = Math.min(cnt - 1, Math.floor(cnt * p));
        return this._withStmt(`
          SELECT latency_ms FROM requests WHERE ts >= ? AND latency_ms > 0
          ORDER BY latency_ms LIMIT 1 OFFSET ?
        `, (sP) => {
          sP.bind([since, off]);
          sP.step();
          return sP.getAsObject().latency_ms || 0;
        });
      };

      const p95 = getPctl(0.95);
      const p99 = getPctl(0.99);

      const getReqCountSince = (secs) => {
        return this._withStmt(`SELECT COUNT(*) AS c FROM requests WHERE ts >= ?`, (sReq) => {
          sReq.bind([now - secs]);
          sReq.step();
          return sReq.getAsObject().c || 0;
        });
      };

      const req_1m = getReqCountSince(60);
      const req_5m = getReqCountSince(300);
      const req_1h = getReqCountSince(3600);
      const req_24h = getReqCountSince(86400);

      return {
        window: windowStr,
        total_requests,
        prompt_tokens: r.prompt_tokens || 0,
        completion_tokens: r.completion_tokens || 0,
        cached_tokens,
        total_tokens,
        cache_hit_pct: cache_pct,
        avg_latency_ms: r.avg_latency_ms ? parseFloat(r.avg_latency_ms.toFixed(1)) : 0.0,
        p95_latency_ms: parseFloat(p95.toFixed(1)),
        p99_latency_ms: parseFloat(p99.toFixed(1)),
        rate_limited_count: r.rate_limited_count || 0,
        total_retries: r.total_retries || 0,
        total_pacing_ms: r.total_pacing_ms ? Math.round(r.total_pacing_ms) : 0,
        paced_requests: r.paced_requests || 0,
        streaming_count: r.streaming_count || 0,
        avg_ttft_ms: r.avg_ttft_ms ? parseFloat(r.avg_ttft_ms.toFixed(1)) : 0.0,
        req_per_min: req_1m,
        req_per_5min: req_5m,
        req_per_hour: req_1h,
        req_per_day: req_24h,
      };
    } catch (e) {
      console.error("metrics.summary error:", e.message);
      return {};
    }
  }

  /** Metrics breakdown per model. */
  getPerModel(windowStr = "24h") {
    try {
      const windowSecs = { "1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 }[windowStr] || 86400;
      const since = (Date.now() / 1000) - windowSecs;
      return this._withStmt(`
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
      `, (stmt) => {
        stmt.bind([since]);
        const out = [];
        while (stmt.step()) {
          const d = stmt.getAsObject();
          const req = d.requests || 0;
          d.non_streaming_count = req - (d.streaming_count || 0);
          d.success_rate = req ? parseFloat(((d.success_count || 0) / req * 100).toFixed(1)) : 0.0;
          d.avg_total_tokens = req ? Math.round((d.total_tokens || 0) / req) : 0;
          d.avg_output_tokens = req ? Math.round((d.completion_tokens || 0) / req) : 0;
          const tt = d.total_tokens || 0;
          d.cache_hit_pct = tt ? parseFloat(((d.cached_tokens || 0) / tt * 100).toFixed(1)) : 0.0;
          d.avg_ttft_ms = d.avg_ttft_ms || 0.0;
          out.push(d);
        }
        return out;
      });
    } catch { return []; }
  }

  /** Hourly timeseries for drilldown of one model. */
  getModelTimeseries(model, hours = 24) {
    try {
      const now = Date.now() / 1000;
      const out = [];
      for (let i = hours; i > 0; i--) {
        const start = now - i * 3600;
        const end = now - (i - 1) * 3600;
        const row = this._withStmt(`
          SELECT COUNT(*) AS req,
                 COALESCE(SUM(total_tokens), 0) AS tok,
                 COALESCE(SUM(was_rate_limited), 0) AS rl
          FROM requests WHERE model = ? AND ts >= ? AND ts < ?
        `, (stmt) => {
          stmt.bind([model, start, end]);
          stmt.step();
          return stmt.getAsObject();
        });
        out.push({
          hour_ago: i,
          requests: row.req || 0,
          total_tokens: row.tok || 0,
          rate_limited: row.rl || 0
        });
      }
      return out;
    } catch { return []; }
  }

  /** Metrics breakdown per API key. */
  getPerKey(windowStr = "24h") {
    try {
      const windowSecs = { "1m": 60, "5m": 300, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 }[windowStr] || 86400;
      const since = (Date.now() / 1000) - windowSecs;
      return this._withStmt(`
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
      `, (stmt) => {
        stmt.bind([since]);
        const out = [];
        while (stmt.step()) {
          out.push(stmt.getAsObject());
        }
        return out;
      });
    } catch { return []; }
  }

  _bucketChart(bucketSecs, n) {
    try {
      const now = Date.now() / 1000;
      const out = [];
      for (let i = n; i > 0; i--) {
        const start = now - i * bucketSecs;
        const end = now - (i - 1) * bucketSecs;
        const row = this._withStmt(`
          SELECT COUNT(*) AS req,
                 COALESCE(SUM(total_tokens), 0) AS tok,
                 COALESCE(SUM(was_rate_limited), 0) AS rl
          FROM requests WHERE ts >= ? AND ts < ?
        `, (stmt) => {
          stmt.bind([start, end]);
          stmt.step();
          return stmt.getAsObject();
        });
        out.push({
          index: n - i,
          requests: row.req || 0,
          total_tokens: row.tok || 0,
          rate_limited: row.rl || 0
        });
      }
      return out;
    } catch { return []; }
  }

  getHourlyChart(hours = 24) {
    const data = this._bucketChart(3600, hours);
    for (const d of data) {
      d.hour_ago = hours - d.index;
      delete d.index;
    }
    return data;
  }

  getDailyChart(days = 30) {
    const data = this._bucketChart(86400, days);
    for (const d of data) {
      d.day_ago = days - d.index;
      delete d.index;
    }
    return data;
  }

  getTotalCounts() {
    try {
      const r = this._withStmt(`
        SELECT COUNT(*) AS total_requests,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(was_rate_limited), 0) AS total_rl_events
        FROM requests
      `, (stmt) => {
        stmt.step();
        return stmt.getAsObject();
      });

      const rl = this._withStmt(`SELECT COUNT(*) AS c FROM rate_limit_events`, (stmt2) => {
        stmt2.step();
        return stmt2.getAsObject().c || 0;
      });

      return {
        all_time_requests: r.total_requests || 0,
        all_time_tokens: r.total_tokens || 0,
        all_time_rl_requests: r.total_rl_events || 0,
        all_time_rl_events: rl
      };
    } catch {
      return { all_time_requests: 0, all_time_tokens: 0, all_time_rl_requests: 0, all_time_rl_events: 0 };
    }
  }

  resetAll() {
    try {
      const n_req = this._withStmt("SELECT COUNT(*) AS c FROM requests", (s1) => {
        s1.step();
        return s1.getAsObject().c || 0;
      });

      const n_rl = this._withStmt("SELECT COUNT(*) AS c FROM rate_limit_events", (s2) => {
        s2.step();
        return s2.getAsObject().c || 0;
      });

      this._db.run("DELETE FROM requests");
      this._db.run("DELETE FROM rate_limit_events");
      this._save();
      console.log(`[metrics] Reset: removed ${n_req} requests, ${n_rl} rate-limit events`);
      return { requests_removed: n_req, rate_limit_events_removed: n_rl };
    } catch (e) {
      console.error("metrics.resetAll error:", e.message);
      return { requests_removed: 0, rate_limit_events_removed: 0 };
    }
  }

  close() {
    try {
      if (this._saveInterval) clearInterval(this._saveInterval);
      this._save(true); // synchronous save before closing
      const db = this._db;
      this._db = null; // Set to null so any pending saves exit early
      db?.close();
    } catch {}
  }
}

module.exports = { Metrics };