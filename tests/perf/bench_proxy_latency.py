#!/usr/bin/env python3
"""Measure wrapper overhead versus direct upstream for OpenAI-compatible APIs.

This script is intentionally dependency-light (aiohttp only, already required by
wrappers). It measures non-stream total latency and streaming TTFT/total latency.

Example:
  python tests/perf/bench_proxy_latency.py \
    --wrapper-base http://127.0.0.1:9104/v1 \
    --direct-base https://api.blackbox.ai \
    --api-key "$BLACKBOX_API_KEY_1" \
    --model blackboxai/nvidia/nemotron-3-super-120b-a12b:free \
    --requests 20 --concurrency 5 --stream
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from dataclasses import dataclass

import aiohttp


@dataclass
class Sample:
    ok: bool
    total_ms: float
    ttft_ms: float | None = None
    status: int = 0
    error: str = ""


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((q / 100.0) * (len(values) - 1)))))
    return values[idx]


async def call_chat(session: aiohttp.ClientSession, base: str, api_key: str, model: str, stream: bool) -> Sample:
    url = base.rstrip('/') + '/chat/completions'
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}],
        'max_tokens': 8,
        'stream': stream,
    }
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    t0 = time.perf_counter()
    ttft = None
    try:
        async with session.post(url, headers=headers, json=body) as resp:
            if not stream:
                text = await resp.text()
                total = (time.perf_counter() - t0) * 1000
                return Sample(resp.status < 400, total, status=resp.status, error='' if resp.status < 400 else text[:300])
            async for chunk in resp.content.iter_any():
                if ttft is None and chunk.strip():
                    ttft = (time.perf_counter() - t0) * 1000
                if b'[DONE]' in chunk:
                    break
            total = (time.perf_counter() - t0) * 1000
            return Sample(resp.status < 400, total, ttft, resp.status)
    except Exception as e:
        return Sample(False, (time.perf_counter() - t0) * 1000, ttft, 0, str(e)[:300])


async def run_many(base: str, api_key: str, model: str, n: int, concurrency: int, stream: bool) -> list[Sample]:
    timeout = aiohttp.ClientTimeout(total=1800, sock_connect=30)
    connector = aiohttp.TCPConnector(limit=max(concurrency * 2, 10), limit_per_host=max(concurrency * 2, 10), ttl_dns_cache=300)
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def one():
            async with sem:
                return await call_chat(session, base, api_key, model, stream)
        return await asyncio.gather(*(one() for _ in range(n)))


def summarize(name: str, samples: list[Sample]):
    ok = [s for s in samples if s.ok]
    totals = [s.total_ms for s in ok]
    ttfts = [s.ttft_ms for s in ok if s.ttft_ms is not None]
    print(f'[{name}] ok={len(ok)}/{len(samples)}')
    if totals:
        print(f'  total_ms p50={pct(totals,50):.1f} p95={pct(totals,95):.1f} p99={pct(totals,99):.1f} mean={statistics.mean(totals):.1f}')
    if ttfts:
        print(f'  ttft_ms  p50={pct(ttfts,50):.1f} p95={pct(ttfts,95):.1f} p99={pct(ttfts,99):.1f} mean={statistics.mean(ttfts):.1f}')
    errors = [s.error for s in samples if not s.ok and s.error]
    if errors:
        print('  sample_error=', errors[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrapper-base', required=True, help='e.g. http://127.0.0.1:9104/v1')
    ap.add_argument('--direct-base', required=True, help='e.g. https://api.blackbox.ai')
    ap.add_argument('--api-key', default=os.environ.get('API_KEY') or os.environ.get('BLACKBOX_API_KEY_1') or 'local')
    ap.add_argument('--model', required=True)
    ap.add_argument('--requests', type=int, default=20)
    ap.add_argument('--concurrency', type=int, default=5)
    ap.add_argument('--stream', action='store_true')
    args = ap.parse_args()

    async def runner():
        direct = await run_many(args.direct_base, args.api_key, args.model, args.requests, args.concurrency, args.stream)
        wrapper = await run_many(args.wrapper_base, args.api_key, args.model, args.requests, args.concurrency, args.stream)
        summarize('direct', direct)
        summarize('wrapper', wrapper)
        d_ok = [s.total_ms for s in direct if s.ok]
        w_ok = [s.total_ms for s in wrapper if s.ok]
        if d_ok and w_ok:
            print(f'[overhead] total_p50_delta_ms={pct(w_ok,50)-pct(d_ok,50):.1f} total_p95_delta_ms={pct(w_ok,95)-pct(d_ok,95):.1f}')
        d_ttft = [s.ttft_ms for s in direct if s.ok and s.ttft_ms is not None]
        w_ttft = [s.ttft_ms for s in wrapper if s.ok and s.ttft_ms is not None]
        if d_ttft and w_ttft:
            print(f'[overhead] ttft_p50_delta_ms={pct(w_ttft,50)-pct(d_ttft,50):.1f} ttft_p95_delta_ms={pct(w_ttft,95)-pct(d_ttft,95):.1f}')

    asyncio.run(runner())


if __name__ == '__main__':
    main()
