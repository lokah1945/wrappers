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

_batch = []
_last_flush = time.time()


async def push_chunk() -> None:
    global _batch, _last_flush
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
        async with aiohttp.ClientSession() as session:
            async with session.post(
                LOKI_URL,
                data=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=TLS_VERIFY if TLS_VERIFY else False,
            ) as resp:
                if 200 <= resp.status < 300:
                    _batch = _batch[len(snapshot):]
                    print(f"[loki_push] flushed {len(snapshot)} records")
                else:
                    print(f"[loki_push] HTTP {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"[loki_push] error: {e}", file=sys.stderr)

    _last_flush = time.time()


def process_line(line: str) -> None:
    line = line.strip()
    if not line:
        return
    _batch.append(line)
    if len(_batch) >= BATCH_SIZE:
        asyncio.create_task(push_chunk())


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
