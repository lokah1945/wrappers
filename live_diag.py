#!/usr/bin/env python3
"""Raw diagnostics for live smoke failures — dumps bodies for root-causing."""
import asyncio
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path('/root/wrapper')


def token_for(name):
    env = (ROOT / name / '.env').read_text()
    m = re.search(r'^BEARER_TOKEN=(.+)$', env, re.M)
    return m.group(1).strip() if m else ''


async def drive(s, url, headers, payload, timeout=90):
    async with s.post(url, json=payload, headers=headers,
                      timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return r.status, await r.text()


async def main():
    cases = []
    # 1. nvidia thinking 400 - error body
    cases.append(('nvidia thinking 400', 9101, 'nvidia-python', '/v1/messages', {
        'model': 'nvidia/nemotron-3-super-120b-a12b', 'max_tokens': 200, 'stream': True,
        'thinking': {'type': 'enabled', 'budget_tokens': 1024},
        'messages': [{'role': 'user', 'content': 'Is 17 prime? One sentence.'}]}))
    # 2. nvidia thinking VALID (budget < max_tokens)
    cases.append(('nvidia thinking valid', 9101, 'nvidia-python', '/v1/messages', {
        'model': 'nvidia/nemotron-3-super-120b-a12b', 'max_tokens': 2048, 'stream': True,
        'thinking': {'type': 'enabled', 'budget_tokens': 512},
        'messages': [{'role': 'user', 'content': 'Is 17 prime? One sentence.'}]}))
    # 3. nous chat stream+tools - raw frames
    cases.append(('nous chat stream tools', 9102, 'nous', '/v1/chat/completions', {
        'model': 'poolside/laguna-s-2.1:free', 'max_tokens': 96, 'stream': True,
        'tools': [{'type': 'function', 'function': {'name': 'get_weather',
                   'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}],
        'messages': [{'role': 'user', 'content': 'Call get_weather for Jakarta.'}]}))
    # 4. nous responses stream - raw tail
    cases.append(('nous responses stream', 9102, 'nous', '/v1/responses', {
        'model': 'poolside/laguna-s-2.1:free', 'max_output_tokens': 48, 'stream': True,
        'input': 'Reply with exactly: WRAP-OK'}))
    # 5. openrouter messages tools - raw body
    cases.append(('openrouter messages tools', 9103, 'openrouter', '/v1/messages', {
        'model': 'nvidia/nemotron-3.5-lightning:free', 'max_tokens': 128,
        'tools': [{'name': 'get_weather', 'description': 'weather lookup',
                   'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string'}},
                                    'required': ['city']}}],
        'messages': [{'role': 'user', 'content': 'Use get_weather for Jakarta.'}]}))
    # 6. openrouter responses nonstream - raw body
    cases.append(('openrouter responses nonstream', 9103, 'openrouter', '/v1/responses', {
        'model': 'nvidia/nemotron-3.5-lightning:free', 'max_output_tokens': 48,
        'input': 'Reply with exactly: WRAP-OK'}))
    # 7. openrouter responses stream - raw tail
    cases.append(('openrouter responses stream', 9103, 'openrouter', '/v1/responses', {
        'model': 'nvidia/nemotron-3.5-lightning:free', 'max_output_tokens': 48, 'stream': True,
        'input': 'Reply with exactly: WRAP-OK'}))

    async with aiohttp.ClientSession() as s:
        for label, port, name, path, payload in cases:
            headers = {'Authorization': f'Bearer {token_for(name)}',
                       'Content-Type': 'application/json'}
            try:
                st, raw = await drive(s, f'http://127.0.0.1:{port}{path}', headers, payload)
            except Exception as e:
                print(f'### {label}: EXC {type(e).__name__}: {e}')
                continue
            print(f'### {label}: HTTP {st}, {len(raw)} bytes')
            if len(raw) > 1600:
                print('--- head 700 ---')
                print(raw[:700])
                print('--- tail 700 ---')
                print(raw[-700:])
            else:
                print(raw)
            print()


if __name__ == '__main__':
    asyncio.run(main())
