#!/usr/bin/env python3
"""Model availability probe: sample real inference across each wrapper's /v1/models.

Runs ON the VPS. For each active wrapper, take a rotating sample of models
and run one tiny chat completion (non-stream, max_tokens=8). Report per-model
status. Goal: every advertised model must be actually usable.
"""
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path('/root/wrapper')
WRAPPERS = {
    'nvidia-python': 9101,
    'nous': 9102,
    'openrouter': 9106,
}
# How many models to probe per wrapper (spread across the list)
SAMPLE_SIZES = {'nvidia-python': 12, 'nous': 7, 'openrouter': 8}


def token_for(name):
    env = (ROOT / name / '.env').read_text()
    for line in env.splitlines():
        if line.startswith('BEARER_TOKEN='):
            return line.split('=', 1)[1].strip()
    return ''


async def probe(session, port, token, model):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8, "stream": False}
    try:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json=body, timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            text = await resp.text()
            if resp.status == 200:
                try:
                    d = json.loads(text)
                    finish = (d.get("choices") or [{}])[0].get("finish_reason")
                    return ("OK", f"finish={finish}")
                except json.JSONDecodeError:
                    return ("OK?", "non-json 200")
            snippet = text[:110].replace("\n", " ")
            return (f"HTTP {resp.status}", snippet)
    except asyncio.TimeoutError:
        return ("TIMEOUT", ">90s")
    except Exception as e:
        return ("EXC", f"{type(e).__name__}: {e}"[:90])


async def main() -> int:
    overall_bad = 0
    async with aiohttp.ClientSession() as session:
        for name, port in WRAPPERS.items():
            token = token_for(name)
            async with session.get(
                f"http://127.0.0.1:{port}/v1/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                models = [m["id"] for m in (await resp.json()).get("data", [])]
            n = SAMPLE_SIZES.get(name, 5)
            step = max(1, len(models) // n)
            sample = models[::step][:n]
            print(f"── {name} ({len(models)} models, probing {len(sample)}) ──")
            for m in sample:
                status, detail = await probe(session, port, token, m)
                flag = "PASS" if status == "OK" else "FAIL"
                if status != "OK":
                    overall_bad += 1
                print(f"{flag} | {m} | {status} | {detail}")
    print(f"\nTOTAL failures: {overall_bad}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
