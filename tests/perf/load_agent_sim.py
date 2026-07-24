#!/usr/bin/env python3
"""Mixed agent/client load simulator for wrapper contract surfaces.

It sends a realistic mix of model discovery, count_tokens, Chat Completions,
Responses API, and Anthropic Messages requests. It is meant for staging/soak
runs, not for unit tests.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import time

import aiohttp


def percentile(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values)-1, max(0, int(round((q/100)*(len(values)-1)))))]


async def request_json(session, method, url, api_key, payload=None):
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    t0 = time.perf_counter()
    try:
        async with session.request(method, url, headers=headers, json=payload) as resp:
            text = await resp.text()
            return resp.status < 400, (time.perf_counter()-t0)*1000, resp.status, text[:300]
    except Exception as e:
        return False, (time.perf_counter()-t0)*1000, 0, str(e)[:300]


async def request_stream(session, url, api_key, payload):
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    t0 = time.perf_counter()
    ttft = None
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            async for chunk in resp.content.iter_any():
                if ttft is None and chunk.strip():
                    ttft = (time.perf_counter()-t0)*1000
                if b'[DONE]' in chunk or b'message_stop' in chunk:
                    break
            return resp.status < 400, (time.perf_counter()-t0)*1000, resp.status, '', ttft
    except Exception as e:
        return False, (time.perf_counter()-t0)*1000, 0, str(e)[:300], ttft


async def run(base_url, api_key, model, requests, concurrency, stream_ratio):
    base = base_url.rstrip('/')
    timeout = aiohttp.ClientTimeout(total=1800, sock_connect=30)
    connector = aiohttp.TCPConnector(limit=concurrency*4, limit_per_host=concurrency*4, ttl_dns_cache=300)
    sem = asyncio.Semaphore(concurrency)
    results = []
    ttfts = []

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def one(i):
            async with sem:
                r = random.random()
                if r < 0.10:
                    ok, ms, st, err = await request_json(session, 'GET', f'{base}/models', api_key)
                elif r < 0.20:
                    ok, ms, st, err = await request_json(session, 'POST', f'{base}/messages/count_tokens', api_key, {'model': model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'hello'}]})
                elif r < 0.45:
                    payload = {'model': model, 'input': 'Reply OK', 'max_output_tokens': 8, 'stream': random.random() < stream_ratio}
                    if payload['stream']:
                        ok, ms, st, err, ttft = await request_stream(session, f'{base}/responses', api_key, payload)
                        if ttft is not None:
                            ttfts.append(ttft)
                    else:
                        ok, ms, st, err = await request_json(session, 'POST', f'{base}/responses', api_key, payload)
                elif r < 0.70:
                    payload = {'model': model, 'max_tokens': 8, 'stream': random.random() < stream_ratio, 'messages': [{'role': 'user', 'content': 'Reply OK'}]}
                    if payload['stream']:
                        ok, ms, st, err, ttft = await request_stream(session, f'{base}/messages', api_key, payload)
                        if ttft is not None:
                            ttfts.append(ttft)
                    else:
                        ok, ms, st, err = await request_json(session, 'POST', f'{base}/messages', api_key, payload)
                else:
                    payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK'}], 'max_tokens': 8, 'stream': random.random() < stream_ratio}
                    if payload['stream']:
                        ok, ms, st, err, ttft = await request_stream(session, f'{base}/chat/completions', api_key, payload)
                        if ttft is not None:
                            ttfts.append(ttft)
                    else:
                        ok, ms, st, err = await request_json(session, 'POST', f'{base}/chat/completions', api_key, payload)
                results.append((ok, ms, st, err))
        await asyncio.gather(*(one(i) for i in range(requests)))
    return results, ttfts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-url', required=True, help='Wrapper base URL, e.g. http://127.0.0.1:9104/v1')
    ap.add_argument('--api-key', default=os.environ.get('API_KEY') or os.environ.get('BLACKBOX_API_KEY_1') or 'local')
    ap.add_argument('--model', required=True)
    ap.add_argument('--requests', type=int, default=100)
    ap.add_argument('--concurrency', type=int, default=10)
    ap.add_argument('--stream-ratio', type=float, default=0.5)
    args = ap.parse_args()
    results, ttfts = asyncio.run(run(args.base_url, args.api_key, args.model, args.requests, args.concurrency, args.stream_ratio))
    ok = [ms for ok, ms, st, err in results if ok]
    bad = [(st, err) for ok, ms, st, err in results if not ok]
    print(f'ok={len(ok)}/{len(results)} error={len(bad)}')
    if ok:
        print(f'latency_ms p50={percentile(ok,50):.1f} p95={percentile(ok,95):.1f} p99={percentile(ok,99):.1f} mean={statistics.mean(ok):.1f}')
    if ttfts:
        print(f'ttft_ms p50={percentile(ttfts,50):.1f} p95={percentile(ttfts,95):.1f} p99={percentile(ttfts,99):.1f}')
    if bad:
        print('sample_error=', bad[0])


if __name__ == '__main__':
    main()
