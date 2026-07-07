#!/usr/bin/env node
/**
 * loki_push.js — Node.js equivalent of Python loki_push.py
 */
const fs = require('fs');
const http = require('http');
const https = require('https');

const LOKI_URL = process.env.LOKI_PUSH_URL || 'http://127.0.0.1:3100/loki/api/v1/push';
const SOURCE = process.env.LOKI_SOURCE_FILE || 'metrics_data/wrapper-events.jsonl';
const BATCH_SIZE = parseInt(process.env.LOKI_BATCH_SIZE || '50', 10);
const FLUSH_INTERVAL = parseFloat(process.env.LOKI_FLUSH_INTERVAL || '5.0') * 1000;
const LABELS = JSON.parse(process.env.LOKI_LABELS_JSON || '{"job":"wrapper-nvidia"}');
const TENANT = (process.env.LOKI_TENANT_ID || '').trim();
const TLS_VERIFY = process.env.LOKI_TLS_VERIFY === '1';

let batch = [];
let lastFlush = Date.now();

function pushChunk() {
  if (batch.length === 0) return;
  
  // Use monotonic offset to ensure all records in the batch have unique nanosecond timestamps
  let baseTimeNs = Date.now() * 1000000;
  const streams = batch.map((line, idx) => ({
    stream: LABELS,
    values: [[String(baseTimeNs + idx), line.trim()]]
  }));

  const payload = JSON.stringify({ streams });
  const url = new URL(LOKI_URL);
  const mod = url.protocol === 'https:' ? https : http;

  const headers = { 'Content-Type': 'application/json' };
  if (TENANT) headers['X-Scope-OrgID'] = TENANT;

  const req = mod.request(url, {
    method: 'POST',
    headers,
    timeout: 10000,
    rejectUnauthorized: TLS_VERIFY
  }, (res) => {
    if (res.statusCode >= 200 && res.statusCode < 300) {
      console.log(`[loki_push] flushed ${batch.length} records`);
    } else {
      console.error(`[loki_push] HTTP ${res.statusCode}`);
    }
  });

  req.on('error', (e) => console.error(`[loki_push] error: ${e.message}`));
  req.write(payload);
  req.end();
  batch = [];
  lastFlush = Date.now();
}

function processLine(line) {
  if (!line.trim()) return;
  batch.push(line);
  if (batch.length >= BATCH_SIZE) pushChunk();
}

function tail() {
  try {
    const data = fs.readFileSync(SOURCE, 'utf8');
    data.split('\n').forEach(processLine);
  } catch {}
  if (batch.length) pushChunk();
}

function daemon() {
  let pos = 0;
  try { pos = fs.statSync(SOURCE).size; } catch {}
  console.log(`[loki_push] daemon watching ${SOURCE}`);
  setInterval(() => {
    try {
      const stat = fs.statSync(SOURCE);
      if (stat.size <= pos) {
        if (Date.now() - lastFlush >= FLUSH_INTERVAL && batch.length) pushChunk();
        return;
      }
      const fd = fs.openSync(SOURCE, 'r');
      const buf = Buffer.alloc(stat.size - pos);
      fs.readSync(fd, buf, 0, buf.length, pos);
      fs.closeSync(fd);
      pos = stat.size;
      buf.toString('utf8').split('\n').forEach(processLine);
      lastFlush = Date.now();
    } catch {}
    if (Date.now() - lastFlush >= FLUSH_INTERVAL && batch.length) pushChunk();
  }, 500);
}

const mode = process.argv[2] || 'daemon';
if (mode === 'once') { tail(); console.log('[loki_push] once mode done'); }
else { daemon(); }
