#!/usr/bin/env python3
"""Dump RAW upstream chunks from NOUS for step-3.7-flash (thinking off/on)
to determine if the duplication (reasoning == content) comes from upstream."""
import asyncio
import json
import os
import sys

import aiohttp

BASE = os.environ.get("NOUS_BASE_URL", "https://inference-api.nousresearch.com").rstrip("/")
KEY = os.environ.get("NOUS_API_KEY_1") or os.environ.get("BEARER_TOKEN")
NL = chr(10)


async def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "stepfun/step-3.7-flash:free"
    body = {
        "model": model,
        "max_tokens": 300,
        "stream": True,
        "messages": [{"role": "user", "content": "hallo"}],
        "tags": [t.strip() for t in os.environ.get(
            "NOUS_REQUEST_TAGS", "product=wrapper-nous,client=wrapper-nous,user=wrapper-nous").split(",") if t.strip()],
    }
    reasoning_txt = ""
    content_txt = ""
    n_chunks = 0
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{BASE}/v1/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {KEY}"}) as r:
            print(f"HTTP {r.status}")
            if r.status != 200:
                print((await r.text())[:500])
                return
            async for raw in r.content:
                for line in raw.decode("utf-8", "replace").split(NL):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload in ("", "[DONE]"):
                        continue
                    try:
                        ev = json.loads(payload)
                    except ValueError:
                        continue
                    if "choices" not in ev:
                        continue
                    n_chunks += 1
                    d = (ev["choices"] or [{}])[0].get("delta", {}) or {}
                    rc = d.get("reasoning_content") or d.get("reasoning") or ""
                    ct = d.get("content") or ""
                    if isinstance(rc, str):
                        reasoning_txt += rc
                    if isinstance(ct, str):
                        content_txt += ct
    print(f"chunks={n_chunks}")
    print(f"reasoning_content ({len(reasoning_txt)}ch): {reasoning_txt[:200]!r}")
    print(f"content          ({len(content_txt)}ch): {content_txt[:200]!r}")
    print(f"IDENTICAL: {reasoning_txt == content_txt and reasoning_txt != ''}")


if __name__ == "__main__":
    asyncio.run(main())
