#!/usr/bin/env python3
"""Repeat-test: (a) NOUS step-flash thinking-off duplication, (b) OR nano reasoning-off empty.
Runs each scenario N times, dumps raw SSE events on anomaly."""
import asyncio
import json
import sys

import aiohttp

KEY = "wrapper-local-key"
HDRS = {"x-api-key": KEY, "authorization": f"Bearer {KEY}", "anthropic-version": "2023-06-01", "content-type": "application/json"}
NL = chr(10)

SCEN = [
    ("nous-dup", 9102, "stepfun/step-3.7-flash:free", False),
    ("or-empty", 9106, "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", False),
]


async def one(port, model, max_tokens=600):
    body = {"model": model, "max_tokens": max_tokens, "stream": True,
            "messages": [{"role": "user", "content": "hallo"}]}
    url = f"http://127.0.0.1:{port}/v1/messages"
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body, headers=HDRS) as r:
                if r.status != 200:
                    return {"err": f"HTTP {r.status}"}
                events = []
                async for raw in r.content:
                    for line in raw.decode("utf-8", "replace").split(NL):
                        line = line.strip()
                        if line.startswith("data:"):
                            p = line[5:].strip()
                            if p and p != "[DONE]":
                                try:
                                    events.append(json.loads(p))
                                except ValueError:
                                    pass
                return {"events": events}
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}"}


def summarize(events):
    blocks, cur = [], None
    for ev in events:
        t = ev.get("type")
        if t == "content_block_start":
            cb = ev.get("content_block", {})
            cur = {"idx": ev.get("index"), "type": cb.get("type"), "text": ""}
            blocks.append(cur)
        elif t == "content_block_delta":
            if cur is None:
                continue
            d = ev.get("delta", {})
            if d.get("type") == "thinking_delta":
                cur["text"] += d.get("thinking", "")
            elif d.get("type") == "text_delta":
                cur["text"] += d.get("text", "")
        elif t == "content_block_stop":
            cur = None
    return blocks


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for label, port, model, thinking in SCEN:
        print(f"\n### {label} ({model} @ {port}) x{n}")
        for i in range(n):
            r = await one(port, model)
            if "err" in r:
                print(f"  run{i+1}: ERROR {r['err']}")
                continue
            blocks = summarize(r["events"])
            types = [(b["idx"], b["type"], len(b["text"])) for b in blocks]
            anom = ""
            if label == "nous-dup" and len(blocks) == 2 and blocks[0]["text"] and blocks[0]["text"] == blocks[1].get("text"):
                anom = " <-- DUPLICATION"
            if label == "or-empty" and not blocks:
                anom = " <-- EMPTY"
            print(f"  run{i+1}: blocks={types}{anom}")
            if anom and "--dump" in sys.argv:
                print("   RAW EVENTS:")
                for ev in r["events"]:
                    print("    ", json.dumps(ev, ensure_ascii=False)[:220])


if __name__ == "__main__":
    asyncio.run(main())
