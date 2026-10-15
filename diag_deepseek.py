#!/usr/bin/env python3
"""Diagnose deepseek-v4.1-flash timeout on 9101: retry with long timeout,
check whether it's slow upstream or a wrapper bug."""
import asyncio
import json
import time

import aiohttp

KEY = "wrapper-local-key"
HDRS = {"x-api-key": KEY, "authorization": f"Bearer {KEY}", "anthropic-version": "2023-06-01", "content-type": "application/json"}
NL = chr(10)


async def one(port, model, total=300):
    body = {"model": model, "max_tokens": 300, "stream": True,
            "messages": [{"role": "user", "content": "hallo"}]}
    url = f"http://127.0.0.1:{port}/v1/messages"
    t0 = time.time()
    first = None
    n_events = 0
    blocks = {}
    timeout = aiohttp.ClientTimeout(total=total)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body, headers=HDRS) as r:
                if r.status != 200:
                    return f"HTTP {r.status}: {(await r.text())[:150]}"
                async for raw in r.content:
                    for line in raw.decode("utf-8", "replace").split(NL):
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        if first is None:
                            first = time.time() - t0
                        p = line[5:].strip()
                        if p in ("", "[DONE]"):
                            continue
                        try:
                            ev = json.loads(p)
                        except ValueError:
                            continue
                        n_events += 1
                        if ev.get("type") == "content_block_delta":
                            d = ev.get("delta", {})
                            k = d.get("type", "?")
                            blocks[k] = blocks.get(k, 0) + len(str(d.get(k.replace("_delta", ""), d.get("text", ""))))
        return f"OK ttfb={first:.2f}s total={time.time()-t0:.1f}s events={n_events} deltas={blocks}"
    except Exception as e:
        return f"{type(e).__name__} at {time.time()-t0:.1f}s: {e}"


async def main():
    for model, port in [("deepseek-ai/deepseek-v4.1-flash", 9101),
                        ("nvidia/nemotron-3.5-lightning-30b-a3b", 9101)]:
        print(f"[{model}] ", await one(port, model))


if __name__ == "__main__":
    asyncio.run(main())
