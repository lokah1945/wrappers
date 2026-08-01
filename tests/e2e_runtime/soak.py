#!/usr/bin/env python3
"""Sustained-load soak: catches leaks and slow degradation that a single pass
cannot. Boots one wrapper, drives N concurrent agent-shaped streams for a
duration, and asserts:

  * zero request failures
  * RSS does not grow without bound (response-store / task leaks)
  * the key pool never starves (exactly-once release)
  * no tracebacks in the server log
  * p95 latency does not degrade between the first and last quarter

Usage: python tests/e2e_runtime/soak.py --wrapper blackbox --seconds 45 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests' / 'runtime'))
from run_runtime_e2e import (  # noqa: E402
    BASE_FOR, MOCK_PORT, TOKEN, WRAPPERS, free_port_wait, health_wait,
    scan_log, start_wrapper,
)

MODES = ['normal', 'tools', 'reasoning', 'keepalive', 'unicode',
         'nullcontent', 'emptychoices', 'longtool', 'usage_after']


def rss_kb(pid: int) -> int:
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


async def worker(session, base, headers, stop_at, stats, idx):
    surfaces = ['/v1/messages', '/v1/chat/completions', '/v1/responses']
    i = 0
    while time.time() < stop_at:
        mode = MODES[i % len(MODES)]
        surface = surfaces[i % len(surfaces)]
        i += 1
        if surface == '/v1/messages':
            payload = {'model': f'mock/{mode}', 'max_tokens': 128, 'stream': True,
                       'messages': [{'role': 'user', 'content': f'w{idx}-{i}'}]}
        elif surface == '/v1/chat/completions':
            payload = {'model': f'mock/{mode}', 'stream': True,
                       'messages': [{'role': 'user', 'content': f'w{idx}-{i}'}]}
        else:
            payload = {'model': f'mock/{mode}', 'stream': True, 'input': f'w{idx}-{i}'}
        t0 = time.time()
        try:
            async with session.post(base + surface, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.text()
                dt = time.time() - t0
                if r.status == 404:
                    continue
                if r.status == 429:
                    # Correct backpressure, not a defect: the soak deliberately
                    # drives more RPM than a single mock key is configured for.
                    # What matters is that it is a SHAPED error, returned
                    # promptly, and that the pool recovers (asserted post-soak).
                    stats['throttled'] += 1
                    try:
                        d = json.loads(body)
                        if 'error' not in d:
                            stats['fail'].append(f'{surface} {mode} 429 not shaped')
                    except json.JSONDecodeError:
                        stats['fail'].append(f'{surface} {mode} 429 body not JSON')
                    await asyncio.sleep(0.05)
                    continue
                if r.status != 200:
                    stats['fail'].append(
                        f'{surface} {mode} HTTP {r.status}: {body[:160]}')
                    continue
                if not body.strip():
                    stats['fail'].append(f'{surface} {mode} empty body')
                    continue
                # Terminal-event contract
                if surface == '/v1/messages' and 'message_stop' not in body:
                    stats['fail'].append(f'{surface} {mode} no message_stop')
                elif surface == '/v1/chat/completions' and '[DONE]' not in body:
                    stats['fail'].append(f'{surface} {mode} no [DONE]')
                elif surface == '/v1/responses' and (
                        'response.completed' not in body and 'response.failed' not in body):
                    stats['fail'].append(f'{surface} {mode} no terminal response event')
                stats['lat'].append(dt)
                stats['ok'] += 1
        except Exception as e:
            stats['fail'].append(f'{surface} {mode} {type(e).__name__}: {e}')


async def run(wrapper: str, seconds: int, concurrency: int) -> int:
    wdir, port, upstream_var, extra = WRAPPERS[wrapper]
    proc, logf = start_wrapper(wrapper, wdir, port, upstream_var, extra)
    try:
        if not free_port_wait(port) or not health_wait(port):
            print(f'FATAL: {wrapper} did not become healthy')
            return 2
        base = f'http://127.0.0.1:{port}'
        headers = {'Authorization': f'Bearer {TOKEN}',
                   'anthropic-version': '2023-06-01'}
        stats = {'ok': 0, 'fail': [], 'lat': [], 'throttled': 0}

        rss_start = rss_kb(proc.pid)
        stop_at = time.time() + seconds
        conn = aiohttp.TCPConnector(limit=concurrency * 2)
        async with aiohttp.ClientSession(connector=conn) as s:
            await asyncio.gather(*[
                worker(s, base, headers, stop_at, stats, i) for i in range(concurrency)])

            # Post-soak health / pool check
            pool_ok = True
            try:
                async with s.get(base + '/health', headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    h = await r.json()
                    avail = h.get('available', h.get('available_keys'))
                    inflight = sum(k.get('in_flight', 0)
                                   for k in (h.get('live_keys') or []))
                    if avail == 0:
                        pool_ok = False
                        stats['fail'].append(f'key pool starved (available={avail})')
                    if inflight != 0:
                        pool_ok = False
                        stats['fail'].append(f'in_flight leaked: {inflight} after drain')
            except Exception as e:
                pool_ok = False
                stats['fail'].append(f'post-soak health failed: {e}')

        await asyncio.sleep(1.0)
        rss_end = rss_kb(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()

    log_hits = scan_log(wrapper)
    lat = sorted(stats['lat'])
    q = max(1, len(lat) // 4)
    first_q = stats['lat'][:q]
    last_q = stats['lat'][-q:]

    def p95(xs):
        return sorted(xs)[int(len(xs) * 0.95)] if xs else 0.0

    growth = rss_end - rss_start
    print(f'\n── soak: {wrapper} ──')
    print(f'  requests ok      : {stats["ok"]}')
    print(f'  throttled (429)  : {stats["throttled"]}  (shaped backpressure, expected)')
    print(f'  failures         : {len(stats["fail"])}')
    print(f'  p50 / p95 latency: {lat[len(lat)//2]*1000:.0f}ms / {p95(lat)*1000:.0f}ms'
          if lat else '  latency: n/a')
    print(f'  p95 first/last Q : {p95(first_q)*1000:.0f}ms / {p95(last_q)*1000:.0f}ms')
    print(f'  RSS {rss_start//1024}MB -> {rss_end//1024}MB (delta {growth//1024}MB)')
    print(f'  server log issues: {len(log_hits)}')

    bad = False
    for f in stats['fail'][:10]:
        print(f'  ✗ {f}')
        bad = True
    for h in log_hits[:10]:
        print(f'  ✗ log: {h}')
        bad = True
    # 64MB of growth over a short soak indicates an unbounded store/task leak.
    if growth > 64 * 1024:
        print(f'  ✗ RSS grew {growth//1024}MB — probable leak')
        bad = True
    if last_q and first_q and p95(last_q) > max(0.5, p95(first_q) * 5):
        print(f'  ✗ p95 degraded {p95(first_q)*1000:.0f}ms -> {p95(last_q)*1000:.0f}ms')
        bad = True
    if not bad:
        print('  ✅ stable under sustained load')
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrapper')
    ap.add_argument('--seconds', type=int, default=30)
    ap.add_argument('--concurrency', type=int, default=8)
    args = ap.parse_args()

    mock = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(MOCK_PORT)],
        stdout=open('/tmp/soak-mock.log', 'w'), stderr=subprocess.STDOUT)
    if not free_port_wait(MOCK_PORT):
        print('FATAL: mock upstream did not start')
        mock.kill()
        return 2
    rc = 0
    try:
        targets = [args.wrapper] if args.wrapper else list(WRAPPERS)
        for w in targets:
            rc |= asyncio.run(run(w, args.seconds, args.concurrency))
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
    print('\n' + ('❌ soak FAILED' if rc else '✅ soak PASSED for all wrappers'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
