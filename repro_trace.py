#!/usr/bin/env python3
"""Live reproduction: dump raw Anthropic SSE events from wrapper 9101.

Test A: force the model to literally mention <thinking> in prose.
Test B: simple agentic turn (tool use) to catch ``` / newline artifacts.
"""
import asyncio
import json
import sys

import aiohttp

BASE = 'http://localhost:9101'
KEY = 'wrapper-local-key'
MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'

TESTS = {
    'A_literal_tag_mention': [
        {'role': 'user', 'content': 'What does the XML tag <thinking> do in an LLM pipeline? Answer in ONE short paragraph and literally include the tags <thinking> and </thinking> in your explanation.'}
    ],
    'B_simple_turn': [
        {'role': 'user', 'content': 'Reply with exactly: HELLO-WRAPPER-TEST. Nothing else.'}
    ],
}


async def run_test(name, messages):
    events = []
    body = {
        'model': MODEL,
        'max_tokens': 600,
        'messages': messages,
        'stream': True,
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f'{BASE}/v1/messages', json=body,
                                headers={'x-api-key': KEY, 'authorization': f'Bearer {KEY}',
                                         'anthropic-version': '2023-06-01'}) as resp:
            print(f'== {name}: HTTP {resp.status} ==')
            if resp.status != 200:
                print(await resp.text())
                return
            async for raw in resp.content:
                for line in raw.decode('utf-8', errors='replace').split('\n'):
                    line = line.strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        continue
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    events.append(ev)
    # Compact dump of content-relevant events
    for ev in events:
        t = ev.get('type')
        if t == 'content_block_start':
            cb = ev.get('content_block') or {}
            print(f"  START idx={ev.get('index')} type={cb.get('type')}")
        elif t == 'content_block_delta':
            d = ev.get('delta') or {}
            dt = d.get('type')
            if dt == 'text_delta':
                print(f"  TEXT idx={ev.get('index')}: {d.get('text')!r}")
            elif dt == 'thinking_delta':
                print(f"  THINK idx={ev.get('index')}: {d.get('thinking')!r}")
            elif dt == 'input_json_delta':
                print(f"  TOOLJSON idx={ev.get('index')}: {d.get('partial_json')!r}")
        elif t == 'content_block_stop':
            print(f"  STOP idx={ev.get('index')}")
        elif t == 'message_start':
            pass
        elif t == 'message_delta':
            print(f"  MSGDELTA stop_reason={ev.get('delta', {}).get('stop_reason')}")
        elif t == 'message_stop':
            print('  MSGSTOP')
        elif t == 'error':
            print(f"  ERROR: {ev}")
    # Artifact summary
    texts, thinks = [], []
    cur_type = None
    for ev in events:
        t = ev.get('type')
        if t == 'content_block_start':
            cur_type = (ev.get('content_block') or {}).get('type')
            if cur_type == 'text':
                texts.append('')
            elif cur_type == 'thinking':
                thinks.append('')
        elif t == 'content_block_delta':
            d = ev.get('delta') or {}
            if cur_type == 'text' and d.get('type') == 'text_delta':
                texts[-1] += d.get('text') or ''
            elif cur_type == 'thinking' and d.get('type') == 'thinking_delta':
                thinks[-1] += d.get('thinking') or ''
    print(f'-- {name} SUMMARY: {len(texts)} text blocks, {len(thinks)} thinking blocks')
    for i, tx in enumerate(texts):
        print(f'   text[{i}] ({len(tx)}ch): {tx[:160]!r}')
    for i, tk in enumerate(thinks):
        print(f'   think[{i}] ({len(tk)}ch): {tk[:160]!r}')
    # flag artifacts
    for i, tx in enumerate(texts):
        if tx.rstrip() != tx or tx.strip() == '' or '```' in tx:
            print(f'   *** ARTIFACT text[{i}]: {tx!r}')
    for i, tk in enumerate(thinks):
        if '```' in tk:
            print(f'   *** ARTIFACT think[{i}] contains ```')


async def main():
    names = sys.argv[1:] or list(TESTS)
    for name in names:
        if name in TESTS:
            await run_test(name, TESTS[name])


if __name__ == '__main__':
    asyncio.run(main())
