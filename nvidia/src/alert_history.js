#!/usr/bin/env node
/**
 * alert_history.js — separate historian for alert-grade events.
 */
const fs = require('fs');
const path = require('path');

const SOURCE = process.env.ALERT_SOURCE || '/root/wrapper/nvidia/metrics_data/wrapper-events.jsonl';
const OUTPUT = process.env.ALERT_OUTPUT || '/root/wrapper/nvidia/metrics_data/alert-history.jsonl';
const DEDUPE_WINDOW = parseFloat(process.env.ALERT_DEDUPE_WINDOW || '30') * 1000; // ms

// Regular expressions to match alerts based on messages
const RX_429 = /429|rate.limit/i;
const RX_EXHAUST = /exhaust|all.*keys.*failed/i;
const RX_5XX = /5\d\d|upstream.*5\d\d/i;
const RX_UNAVAIL = /unavailable|model.*not.*found/i;
const RX_PACING = /pacing|throttle|enabled_pacing/i;
const RX_DISABLE = /key_disabled|key.*disabled/i;

const dedupe = new Map();

function shouldEmit(kind, model) {
  const key = `${kind}:${model || ''}`;
  const last = dedupe.get(key) || 0;
  const now = Date.now();
  if (now - last < DEDUPE_WINDOW) return false;
  dedupe.set(key, now);
  return true;
}

function classify(ev) {
  const msg = ev.msg || '';
  if (RX_EXHAUST.test(msg)) return { kind: 'exhaustion', severity: 'critical' };
  if (RX_429.test(msg))     return { kind: 'rate_limit', severity: 'warn' };
  if (RX_5XX.test(msg))     return { kind: 'upstream_5xx', severity: 'warn' };
  if (RX_UNAVAIL.test(msg)) return { kind: 'model_unavailable', severity: 'warn' };
  if (RX_PACING.test(msg))   return { kind: 'pacing', severity: 'info' };
  if (RX_DISABLE.test(msg))  return { kind: 'key_disabled', severity: 'warn' };
  return null;
}

function emitAlert(ev, cls) {
  const rec = {
    ts_iso: new Date().toISOString(),
    ts_source: ev.ts,
    kind: cls.kind,
    severity: cls.severity,
    msg: ev.msg,
    model: ev.model,
    key_label: ev.key_label || ev.key,
    attempt: ev.attempt,
    status: ev.status,
    client_ip: ev.client_ip,
    scope: ev.scope,
    in_flight: ev.in_flight,
    scheme: ev.scheme,
    rpm: ev.rpm,
    latency_ms: ev.latency_ms
  };
  // Remove null/undefined fields
  Object.keys(rec).forEach(k => {
    if (rec[k] === undefined || rec[k] === null) delete rec[k];
  });
  return rec;
}

function processLine(line) {
  if (!line.trim()) return;
  try {
    const ev = JSON.parse(line);
    const cls = classify(ev);
    if (!cls) return;
    const rec = emitAlert(ev, cls);
    if (shouldEmit(rec.kind, rec.model)) {
      fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
      fs.appendFileSync(OUTPUT, JSON.stringify(rec) + '\n');
    }
  } catch (e) {
    // Fail-soft: fallback to basic string-matching if not JSON
    const dummyEv = { msg: line, ts: new Date().toISOString() };
    const cls = classify(dummyEv);
    if (cls && shouldEmit(cls.kind, '')) {
      const rec = { ts_iso: dummyEv.ts, kind: cls.kind, severity: cls.severity, msg: line.trim() };
      fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
      fs.appendFileSync(OUTPUT, JSON.stringify(rec) + '\n');
    }
  }
}

function modeOnce() {
  if (!fs.existsSync(SOURCE)) {
    console.error(`[alert_history] source missing: ${SOURCE}`);
    process.exit(2);
  }
  const data = fs.readFileSync(SOURCE, 'utf8');
  const lines = data.split('\n');
  let count = 0;
  for (const line of lines) {
    processLine(line);
    count++;
  }
  console.log(`[alert_history] once: processed ${count} lines`);
}

function modeDaemon() {
  let pos = 0;
  try { pos = fs.statSync(SOURCE).size; } catch {}
  console.log(`[alert_history] daemon mode, watching ${SOURCE} (start_pos=${pos})`);

  // Periodically prune dedupe map to prevent unbounded growth
  setInterval(() => {
    const cutoff = Date.now() - DEDUPE_WINDOW * 2;
    for (const [key, ts] of dedupe) {
      if (ts < cutoff) dedupe.delete(key);
    }
  }, Math.max(DEDUPE_WINDOW, 60000));

  setInterval(() => {
    try {
      if (!fs.existsSync(SOURCE)) return;
      const stat = fs.statSync(SOURCE);
      if (stat.size <= pos) return;

      const fd = fs.openSync(SOURCE, 'r');
      const buf = Buffer.alloc(stat.size - pos);
      fs.readSync(fd, buf, 0, buf.length, pos);
      fs.closeSync(fd);
      pos = stat.size;

      const lines = buf.toString('utf8').split('\n');
      for (const line of lines) {
        processLine(line);
      }
    } catch (e) {
      console.error('[alert_history] daemon read error:', e.message);
    }
  }, 500);
}

function modeTop(n = 20) {
  if (!fs.existsSync(OUTPUT)) {
    console.log("(no alert-history yet)");
    return;
  }
  const data = fs.readFileSync(OUTPUT, 'utf8');
  const lines = data.split('\n');
  const seen = new Map();

  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      const k = `${rec.kind}|${rec.model || 'n/a'}|${rec.severity}`;
      if (!seen.has(k)) {
        seen.set(k, { count: 0, last: rec.ts_iso });
      }
      const entry = seen.get(k);
      entry.count++;
      if (rec.ts_iso > entry.last) entry.last = rec.ts_iso;
    } catch {}
  }

  const byCount = Array.from(seen.entries())
    .map(([k, v]) => {
      const [kind, model, severity] = k.split('|');
      return { kind, model, severity, count: v.count, last: v.last };
    })
    .sort((a, b) => b.count - a.count)
    .slice(0, n);

  for (const item of byCount) {
    const sevStr = item.severity.padEnd(8);
    const kindStr = item.kind.padEnd(18);
    const countStr = String(item.count).padStart(5);
    console.log(`${sevStr} | ${kindStr} | ${countStr} alerts | last: ${item.last} | model=${item.model}`);
  }
}

function modeTail() {
  const { spawn } = require('child_process');
  const tailProc = spawn('tail', ['-n', '100', '-f', OUTPUT], { stdio: 'inherit' });
  tailProc.on('error', (err) => console.error('[alert_history] tail error:', err.message));
}

// Command router
const args = process.argv.slice(2);
const modeArgIndex = args.indexOf('--mode');
let mode = 'daemon';
if (modeArgIndex !== -1 && args[modeArgIndex + 1]) {
  mode = args[modeArgIndex + 1];
} else if (args[0] && !args[0].startsWith('-')) {
  mode = args[0];
}

const nArgIndex = args.indexOf('-n');
let n = 20;
if (nArgIndex !== -1 && args[nArgIndex + 1]) {
  n = parseInt(args[nArgIndex + 1], 10) || 20;
}

if (mode === 'once') {
  modeOnce();
} else if (mode === 'top') {
  modeTop(n);
} else if (mode === 'tail') {
  modeTail();
} else {
  modeDaemon();
}
