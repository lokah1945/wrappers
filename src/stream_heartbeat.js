/**
 * stream_heartbeat.js — SSE comment-line heartbeat writer for long streams.
 *
 * Default: OFF. Enabled via env STREAM_HEARTBEAT=true.
 * Interval default: 5000ms.
 */

'use strict';

const STREAM_HEARTBEAT_ENABLED = (process.env.STREAM_HEARTBEAT || 'false').toLowerCase() === 'true';
const STREAM_HEARTBEAT_INTERVAL_MS = parseInt(process.env.STREAM_HEARTBEAT_INTERVAL_MS || '5000', 10);

let _lastHbAt = 0;

function maybeWriteHeartbeat(res) {
  if (!STREAM_HEARTBEAT_ENABLED) return;
  if (res.writableEnded || res.destroyed) return;
  const now = Date.now();
  if (now - _lastHbAt < STREAM_HEARTBEAT_INTERVAL_MS) return;
  _lastHbAt = now;
  try {
    res.write(`: hb-${now}\n\n`);
  } catch (e) {
    /* socket closed */
  }
}

function installHeartbeatInterval(res) {
  if (!STREAM_HEARTBEAT_ENABLED) return () => {};
  if (!res) return () => {};
  const timer = setInterval(() => maybeWriteHeartbeat(res), 1000);
  // Disarm on stream end
  const cleanup = () => { clearInterval(timer); };
  res.on('close', cleanup);
  res.on('finish', cleanup);
  res.on('error', cleanup);
  // Force first beat
  maybeWriteHeartbeat(res);
  return cleanup;
}

module.exports = {
  STREAM_HEARTBEAT_ENABLED,
  STREAM_HEARTBEAT_INTERVAL_MS,
  maybeWriteHeartbeat,
  installHeartbeatInterval,
};
