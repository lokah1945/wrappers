#!/usr/bin/env python3
"""Dump RAW upstream OpenAI SSE deltas via wrapper 9101 /v1/chat/completions passthrough.

Goal: see exactly what the upstream emits (content vs reasoning_content) for a
tool-calling turn — to locate the origin of trailing ``` artifacts.
"""
import asyncio
import json

import aiohttp

BASE = 'http://localhost:9101'
KEY = 'wrapper-local-key'
MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'


async def main():
    body = {
        'model': MODEL,
        'max_tokens': 500,
        'stream': True,
        'messages': [
            {'role': 'user', 'content': 'Check what Python version is available by running a command. Use the bash tool.'},
        ],
        'tools': [{
            'type': 'function',
            'function': {
                'name': 'bash',
                'description': 'Run a shell command',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'command': {'type': 'string', 'description': 'The command to run'},
                    },
                    'required': ['command'],
                },
            },
        }],
        'tool_choice': 'auto',
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f'{BASE}/v1/chat/completions', json=body,
                                headers={'authorization': f'Bearer {KEY}'}) as resp:
            print(f'HTTP {resp.status}')
            if resp.status != 200:
                print(await resp.text())
                return
            n = 0
            async for raw in resp.content:
                for line in raw.decode('utf-8', errors='replace').split('\n'):
                    line = line.strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        print('[DONE]')
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    ch = (chunk.get('choices') or [{}])[0] or {}
                    delta = ch.get('delta', {}) or {}
                    rc = delta.get('reasoning_content') or delta.get('reasoning')
                    ct = delta.get('content')
                    tc = delta.get('tool_calls')
                    fr = ch.get('finish_reason')
                    parts = []
                    if rc is not None:
                        parts.append(f'RC={rc!r}')
                    if ct is not None:
                        parts.append(f'CT={ct!r}')
                    if tc:
                        parts.append(f'TC={json.dumps(tc)[:200]}')
                    if fr:
                        parts.append(f'FR={fr}')
                    if parts:
                        print(f'#{n} ' + ' '.join(parts))
                    n += 1


if __name__ == '__main__':
    asyncio.run(main())
