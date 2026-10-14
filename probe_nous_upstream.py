#!/usr/bin/env python3
"""Probe the Nous upstream directly: does its tools stream end with [DONE]?"""
import asyncio
import json
import re
from pathlib import Path

import aiohttp

ROOT = Path('/root/wrapper')
ENV = (ROOT / 'nous' / '.env').read_text()
BASE = re.search(r'^NOUS_BASE_URL=(.+)$', ENV, re.M).group(1).strip()
KEY = re.search(r'^NOUS_API_KEY_1=(.+)$', ENV, re.M).group(1).strip()


async def main():
    payload = {
        'model': 'poolside/laguna-s-2.1:free', 'max_tokens': 96, 'stream': True,
        'tools': [{'type': 'function', 'function': {'name': 'get_weather',
                   'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}],
        'messages': [{'role': 'user', 'content': 'Call get_weather for Jakarta.'}]}
    async with aiohttp.ClientSession() as s:
        async with s.post(BASE.rstrip('/') + '/v1/chat/completions', json=payload,
                          headers={'Authorization': f'Bearer {KEY}'},
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            raw = await r.text()
    print(f'HTTP {r.status}, {len(raw)} bytes')
    print('--- tail 400 ---')
    print(raw[-400:])
    print('--- has [DONE]:', 'data: [DONE]' in raw)


if __name__ == '__main__':
    asyncio.run(main())
