#!/usr/bin/env python3
"""
loki_push.py — Python equivalent of Node.js loki_push.js.
Migrated from loki_push.js — functionally identical.

Reads JSONL events from a source file and pushes them to a Loki instance
in batches. Supports once and daemon modes.
"""

import os
import sys
import json
import time
import asyncio

try:
    import aiohttp
except ImportError:
    aiohttp = None

LOKI_URL = os.environ.get('LOKI_PUSH_URL', 'http://127.0.0.1:3100/loki/api/v1/push')
SOURCE = os.environ.get('LOKI_SOURCE_FILE', 'metrics_data/wrapper-events.jsonl')
BATCH_SIZE = int(os.environ.get('LOKI_BATCH_SIZE', '50'))
FLUSH_INTERVAL = float(os.environ.get('LOKI_FLUSH_INTERVAL', '5.0'))
LABELS = json.loads(os.environ.get('LOKI_LABELS_JSON', '{"job":"wrapper-nvidia"}'))
TENANT = os.environ.get('LOKI_TENANT_ID', '').strip()
TLS_VERIFY = os.environ.get('LOKI_TLS_VERIFY', '0') == '1'
# V-04 fix (audit 2026-07-27): bound the retry batch so a failing Loki endpoint
# cannot grow memory without limit, and stop retry-storming on auth failures.
MAX_BUFFER = int(os.environ.get('LOKI_MAX_BUFFER', '1000'))
AUTH_FAILURE_LIMIT = int(os.environ.get('LOKI_AUTH_FAILURE_LIMIT', '3'))

_batch = []
_last_flush = time.time()
_session = None  # BUG-D6 fix: reuse one aiohttp session for all pushes
_auth_failures = 0
_disabled = False  # set after repeated 401/403 — pushes permanently disabled

# F2 round-2 fix: the event loop holds only weak task references; a bare
# create_task(push_chunk()) could be GC'd mid-flight and silently drop a
# batch. Retain a strong reference until the task completes.
_BG_TASKS = set()


def _fire_and_forget(coro, label='bg'):
    """Schedule a background task with a retained reference and error logging."""
    def _done(task):
        _BG_TASKS.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[loki_push] [{label}] background task failed: {exc}", file=sys.stderr)
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # No running event loop — close the coroutine to avoid a warning.
        coro.close()
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_done)


async def _get_session():
    """Reuse one aiohttp session for Loki pushes (BUG-D6 fix)."""
    global _session
    if _session is not None and not _session.closed:
        return _session
    if aiohttp is None:
        return None
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
    )
    return _session


async def push_chunk() -> None:
    global _batch, _last_flush, _auth_failures, _disabled
    if _disabled:
        _batch = []
        return
    if not _batch:
        return
    base_time_ns = int(time.time() * 1_000_000_000)
    snapshot = _batch[:BATCH_SIZE]

    streams = [
        {
            'stream': LABELS,
            'values': [[str(base_time_ns + idx), line.strip()]]
        }
        for idx, line in enumerate(snapshot)
    ]

    payload = json.dumps({'streams': streams})
    headers = {'Content-Type': 'application/json'}
    if TENANT:
        headers['X-Scope-OrgID'] = TENANT

    if aiohttp is None:
        print("[loki_push] aiohttp not available, skipping push", file=sys.stderr)
        _batch = _batch[len(snapshot):]
        return

    try:
        session = await _get_session()
        async with session.post(
            LOKI_URL,
            data=payload,
            headers=headers,
            ssl=TLS_VERIFY if TLS_VERIFY else False,
        ) as resp:
            if 200 <= resp.status < 300:
                _auth_failures = 0
                _batch = _batch[len(snapshot):]
                print(f"[loki_push] flushed {len(snapshot)} records")
            elif resp.status in (401, 403):
                # V-04 fix: repeated auth failures disable pushes (log once)
                # instead of retrying the same rejected batch forever.
                _auth_failures += 1
                _batch = _batch[len(snapshot):]
                if _auth_failures >= AUTH_FAILURE_LIMIT and not _disabled:
                    _disabled = True
                    _batch = []
                    print(f"[loki_push] HTTP {resp.status} auth failure repeated "
                          f"{_auth_failures}x — Loki pushes DISABLED (fix "
                          f"LOKI_TENANT_ID/credentials and restart to re-enable)",
                          file=sys.stderr)
                else:
                    print(f"[loki_push] HTTP {resp.status}", file=sys.stderr)
            elif 400 <= resp.status < 500:
                # Permanent client error: this batch will never be accepted — drop it.
                _batch = _batch[len(snapshot):]
                print(f"[loki_push] HTTP {resp.status} — dropped {len(snapshot)} records", file=sys.stderr)
            else:
                print(f"[loki_push] HTTP {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"[loki_push] error: {e}", file=sys.stderr)

    # V-04 fix: hard cap on the pending batch (drop-oldest).
    if len(_batch) > MAX_BUFFER:
        dropped = len(_batch) - MAX_BUFFER
        _batch = _batch[dropped:]
        print(f"[loki_push] buffer cap {MAX_BUFFER} exceeded — dropped {dropped} oldest records", file=sys.stderr)

    _last_flush = time.time()


def process_line(line: str) -> None:
    global _batch
    if _disabled:
        return
    line = line.strip()
    if not line:
        return
    _batch.append(line)
    # V-04 fix: never let the pending batch grow past the cap (drop-oldest).
    if len(_batch) > MAX_BUFFER:
        _batch = _batch[len(_batch) - MAX_BUFFER:]
    if len(_batch) >= BATCH_SIZE:
        # F2 round-2 fix: retained reference + exception logging.
        _fire_and_forget(push_chunk(), label='push_chunk')


async def tail() -> None:
    try:
        with open(SOURCE, 'r') as f:
            data = f.read()
        for line in data.split('\n'):
            process_line(line)
    except OSError:
        pass
    if _batch:
        await push_chunk()


async def daemon() -> None:
    global _last_flush
    pos = 0
    try:
        pos = os.path.getsize(SOURCE)
    except OSError:
        pass
    print(f"[loki_push] daemon watching {SOURCE}")

    while True:
        await asyncio.sleep(0.5)
        try:
            stat = os.stat(SOURCE)
            if stat.st_size <= pos:
                if time.time() - _last_flush >= FLUSH_INTERVAL and _batch:
                    await push_chunk()
                continue
            with open(SOURCE, 'r') as f:
                f.seek(pos)
                buf = f.read(stat.st_size - pos)
            pos = stat.st_size
            for line in buf.split('\n'):
                process_line(line)
            _last_flush = time.time()
        except OSError:
            pass
        if time.time() - _last_flush >= FLUSH_INTERVAL and _batch:
            await push_chunk()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'daemon'
    if mode == 'once':
        asyncio.run(tail())
        print("[loki_push] once mode done")
    else:
        asyncio.run(daemon())


if __name__ == '__main__':
    main()
