#!/usr/bin/env python3
"""Forensic: does wrapper emit PROPER thinking blocks (Claude Code display verdict)?

User evidence in Claude Code:
    > hallo
    ● The user said "hallo" - this is a simple greeting...
    =======
    Hello! How can I help you today?
    ✻ Brewed for 1s

Question: is the ● reasoning a proper `thinking` content block (normal Claude Code
rendering) or leaked into `text`? Is `=======` in our payload at all?

This script replays Claude Code-style requests (with and without thinking param)
against all active wrappers and dumps the raw content-block structure.
"""
import asyncio
import json
import sys

import aiohttp

KEY = "wrapper-local-key"
HDRS = {
    "x-api-key": KEY,
    "authorization": f"Bearer {KEY}",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}
NL = chr(10)

CASES = [
    # (port, model, label)
    (9101, "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "NVIDIA nano-reasoning"),
    (9101, "deepseek-ai/deepseek-v4.1-flash", "NVIDIA deepseek-flash"),
    (9106, "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "OR nano-reasoning"),
    (9106, "dots-studio/dots-3-note-preview:free", "OR dots-3-note"),
    (9106, "thinkingmachines/inkling-small:free", "OR inkling-small"),
    (9102, "stepfun/step-3.7-flash:free", "NOUS step-flash"),
    (9102, "stepfun/step-5-preview:free", "NOUS step-5"),
]


async def one(port, model, thinking_on, max_tokens=600):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": "hallo"}],
    }
    if thinking_on:
        body["thinking"] = {"type": "enabled", "budget_tokens": 2048}
    url = f"http://127.0.0.1:{port}/v1/messages"
    timeout = aiohttp.ClientTimeout(total=120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body, headers=HDRS) as r:
                if r.status != 200:
                    return {"error": f"HTTP {r.status}: {(await r.text())[:200]}"}
                events = []
                async for raw in r.content:
                    for line in raw.decode("utf-8", "replace").split(NL):
                        line = line.strip()
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if payload and payload != "[DONE]":
                                try:
                                    events.append(json.loads(payload))
                                except ValueError:
                                    pass
                return {"events": events}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def analyze(events):
    """Reconstruct block structure from SSE events."""
    blocks = []  # list of (index, type, text, signature)
    cur = None
    for ev in events:
        t = ev.get("type")
        if t == "content_block_start":
            cb = ev.get("content_block", {})
            cur = {"idx": ev.get("index"), "type": cb.get("type"), "text": "", "sig": False}
            blocks.append(cur)
        elif t == "content_block_delta":
            if cur is None:
                continue
            d = ev.get("delta", {})
            dt = d.get("type")
            if dt == "thinking_delta":
                cur["text"] += d.get("thinking", "")
            elif dt == "signature_delta":
                cur["sig"] = True
            elif dt == "text_delta":
                cur["text"] += d.get("text", "")
        elif t == "content_block_stop":
            cur = None
    return blocks


def verdict(blocks):
    issues = []
    has_think = any(b["type"] == "thinking" for b in blocks)
    has_text = any(b["type"] == "text" for b in blocks)
    for b in blocks:
        if b["type"] == "thinking" and not b["sig"]:
            issues.append("thinking block TANPA signature")
        if b["type"] == "text":
            tx = b["text"]
            if "=======" in tx:
                issues.append("'=======' di text block!")
            if "user said" in tx.lower() or "greeting" in tx.lower() and len(tx) < 250 and not has_think:
                issues.append("reasoning-like content di text block (possible leak)")
    if not blocks:
        issues.append("tidak ada content block")
    return has_think, has_text, issues


async def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for port, model, label in CASES:
        if only and only not in label:
            continue
        for thinking_on in (True, False):
            tag = "THINKING-ON " if thinking_on else "THINKING-OFF"
            print(f"\n{'='*70}\n[{label}] {model} @ {port} — {tag}")
            r = await one(port, model, thinking_on)
            if "error" in r:
                print(f"  ERROR: {r['error']}")
                continue
            blocks = analyze(r["events"])
            has_think, has_text, issues = verdict(blocks)
            print(f"  blocks: {[(b['idx'], b['type'], len(b['text'])) for b in blocks]}")
            for b in blocks:
                preview = b["text"][:160].replace(NL, "\\n")
                print(f"    [{b['idx']}] {b['type']:9s} sig={b['sig']} len={len(b['text'])}: {preview}")
            # raw payload scan for =======
            raw = json.dumps(r["events"])
            eq = "=======" in raw
            print(f"  '=======' di payload SSE: {'YA — SUSPECT!' if eq else 'TIDAK'}")
            status = "OK" if not issues else "ISSUE: " + "; ".join(issues)
            print(f"  VERDICT: {status}")


if __name__ == "__main__":
    asyncio.run(main())
