#!/usr/bin/env python3
"""
alert_history.py — separate historian for alert-grade events.
Migrated from alert_history.js — functionally identical.

Reads JSONL events from a source file, classifies them into alert categories,
and writes deduplicated alerts to an output file. Supports once, daemon,
top, and tail modes.
"""

import os
import re
import sys
import time
import json
import asyncio
from pathlib import Path

SOURCE = os.environ.get('ALERT_SOURCE', '/root/wrapper/nvidia/metrics_data/wrapper-events.jsonl')
OUTPUT = os.environ.get('ALERT_OUTPUT', '/root/wrapper/nvidia/metrics_data/alert-history.jsonl')
DEDUPE_WINDOW = float(os.environ.get('ALERT_DEDUPE_WINDOW', '30')) * 1000  # ms

RX_429 = re.compile(r'429|rate.limit', re.IGNORECASE)
RX_EXHAUST = re.compile(r'exhaust|all.*keys.*failed', re.IGNORECASE)
RX_5XX = re.compile(r'5\d\d|upstream.*5\d\d', re.IGNORECASE)
RX_UNAVAIL = re.compile(r'unavailable|model.*not.*found', re.IGNORECASE)
RX_PACING = re.compile(r'pacing|throttle|enabled_pacing', re.IGNORECASE)
RX_DISABLE = re.compile(r'key_disabled|key.*disabled', re.IGNORECASE)

_dedupe: dict = {}


def should_emit(kind: str, model: str) -> bool:
    key = f"{kind}:{model or ''}"
    last = _dedupe.get(key, 0)
    now = time.time() * 1000
    if now - last < DEDUPE_WINDOW:
        return False
    _dedupe[key] = now
    return True


def classify(ev: dict) -> dict:
    msg = ev.get('msg', '')
    if RX_EXHAUST.search(msg):
        return {'kind': 'exhaustion', 'severity': 'critical'}
    if RX_429.search(msg):
        return {'kind': 'rate_limit', 'severity': 'warn'}
    if RX_5XX.search(msg):
        return {'kind': 'upstream_5xx', 'severity': 'warn'}
    if RX_UNAVAIL.search(msg):
        return {'kind': 'model_unavailable', 'severity': 'warn'}
    if RX_PACING.search(msg):
        return {'kind': 'pacing', 'severity': 'info'}
    if RX_DISABLE.search(msg):
        return {'kind': 'key_disabled', 'severity': 'warn'}
    return None


def emit_alert(ev: dict, cls: dict) -> dict:
    rec = {
        'ts_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'ts_source': ev.get('ts'),
        'kind': cls['kind'],
        'severity': cls['severity'],
        'msg': ev.get('msg'),
        'model': ev.get('model'),
        'key_label': ev.get('key_label') or ev.get('key'),
        'attempt': ev.get('attempt'),
        'status': ev.get('status'),
        'client_ip': ev.get('client_ip'),
        'scope': ev.get('scope'),
        'in_flight': ev.get('in_flight'),
        'scheme': ev.get('scheme'),
        'rpm': ev.get('rpm'),
        'latency_ms': ev.get('latency_ms'),
    }
    return {k: v for k, v in rec.items() if v is not None}


def process_line(line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
        cls = classify(ev)
        if not cls:
            return
        rec = emit_alert(ev, cls)
        if should_emit(rec['kind'], rec.get('model', '')):
            Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT, 'a') as f:
                f.write(json.dumps(rec) + '\n')
    except (json.JSONDecodeError, ValueError):
        dummy_ev = {'msg': line, 'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        cls = classify(dummy_ev)
        if cls and should_emit(cls['kind'], ''):
            rec = {'ts_iso': dummy_ev['ts'], 'kind': cls['kind'], 'severity': cls['severity'], 'msg': line}
            Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT, 'a') as f:
                f.write(json.dumps(rec) + '\n')


def mode_once() -> None:
    if not os.path.exists(SOURCE):
        print(f"[alert_history] source missing: {SOURCE}", file=sys.stderr)
        sys.exit(2)
    with open(SOURCE, 'r') as f:
        lines = f.read().split('\n')
    count = 0
    for line in lines:
        process_line(line)
        count += 1
    print(f"[alert_history] once: processed {count} lines")


async def mode_daemon() -> None:
    pos = 0
    try:
        pos = os.path.getsize(SOURCE)
    except OSError:
        pass
    print(f"[alert_history] daemon mode, watching {SOURCE} (start_pos={pos})")

    async def prune_dedupe():
        while True:
            await asyncio.sleep(max(DEDUPE_WINDOW / 1000, 60))
            cutoff = time.time() * 1000 - DEDUPE_WINDOW * 2
            for key in list(_dedupe.keys()):
                if _dedupe[key] < cutoff:
                    del _dedupe[key]

    asyncio.create_task(prune_dedupe())

    while True:
        await asyncio.sleep(0.5)
        try:
            if not os.path.exists(SOURCE):
                continue
            stat = os.stat(SOURCE)
            if stat.st_size <= pos:
                continue
            with open(SOURCE, 'r') as f:
                f.seek(pos)
                buf = f.read(stat.st_size - pos)
            pos = stat.st_size
            for line in buf.split('\n'):
                process_line(line)
        except Exception as e:
            print(f"[alert_history] daemon read error: {e}", file=sys.stderr)


def mode_top(n: int = 20) -> None:
    if not os.path.exists(OUTPUT):
        print("(no alert-history yet)")
        return
    with open(OUTPUT, 'r') as f:
        lines = f.read().split('\n')
    seen = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            k = f"{rec.get('kind', '')}|{rec.get('model', 'n/a')}|{rec.get('severity', '')}"
            if k not in seen:
                seen[k] = {'count': 0, 'last': rec.get('ts_iso', '')}
            entry = seen[k]
            entry['count'] += 1
            if rec.get('ts_iso', '') > entry['last']:
                entry['last'] = rec['ts_iso']
        except (json.JSONDecodeError, ValueError):
            pass

    by_count = sorted(
        [{'kind': k.split('|')[0], 'model': k.split('|')[1], 'severity': k.split('|')[2],
          'count': v['count'], 'last': v['last']} for k, v in seen.items()],
        key=lambda x: x['count'],
        reverse=True,
    )[:n]

    for item in by_count:
        sev_str = item['severity'].ljust(8)
        kind_str = item['kind'].ljust(18)
        count_str = str(item['count']).rjust(5)
        print(f"{sev_str} | {kind_str} | {count_str} alerts | last: {item['last']} | model={item['model']}")


def mode_tail() -> None:
    import subprocess
    try:
        proc = subprocess.Popen(['tail', '-n', '100', '-f', OUTPUT], stdout=sys.stdout, stderr=sys.stderr)
        proc.wait()
    except Exception as e:
        print(f"[alert_history] tail error: {e}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    mode = 'daemon'
    mode_idx = -1
    for i, a in enumerate(args):
        if a == '--mode':
            mode_idx = i
            break
    if mode_idx != -1 and mode_idx + 1 < len(args):
        mode = args[mode_idx + 1]
    elif args and not args[0].startswith('-'):
        mode = args[0]

    n = 20
    n_idx = -1
    for i, a in enumerate(args):
        if a == '-n':
            n_idx = i
            break
    if n_idx != -1 and n_idx + 1 < len(args):
        n = int(args[n_idx + 1]) or 20

    if mode == 'once':
        mode_once()
    elif mode == 'top':
        mode_top(n)
    elif mode == 'tail':
        mode_tail()
    else:
        asyncio.run(mode_daemon())


if __name__ == '__main__':
    main()
