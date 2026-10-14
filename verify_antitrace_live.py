#!/usr/bin/env python3
"""End-to-end live verification of the anti-trace fix on wrapper 9101.

Mirrors the empirical failure modes found in the Claude Code transcripts:
A. literal tag mentions in user question -> reasoning must stay whole,
   answer text must not contain leaked reasoning fragments.
B. normal turn -> clean text/thinking separation.
C. multi-chunk split tags in content (synthetic upstream).
D. real nemotron agentic turn (prose + tool_use) -> no stray closing tags,
   no standalone newline text blocks.
E. non-stream surface -> clean content.
"""
import asyncio
import json

import aiohttp

BASE = 'http://localhost:9101'
KEY = 'wrapper-local-key'
MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'

CLOSE = chr(60) + '/think' + chr(62)      # 8-char closing tag
NL = chr(10)


async def collect_events(messages, stream=True, max_tokens=800, tools=None):
    body = {'model': MODEL, 'max_tokens': max_tokens, 'messages': messages, 'stream': stream}
    if tools:
        body['tools'] = tools
    events = []
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f'{BASE}/v1/messages', json=body,
                                headers={'x-api-key': KEY, 'authorization': f'Bearer {KEY}',
                                         'anthropic-version': '2023-06-01'}) as resp:
            if resp.status != 200:
                return resp.status, await resp.text(), []
            if not stream:
                data = await resp.json()
                return 200, data, []
            async for raw in resp.content:
                for line in raw.decode('utf-8', errors='replace').split(NL):
                    line = line.strip()
                    if line.startswith('data:'):
                        data = line[5:].strip()
                        if data and data != '[DONE]':
                            try:
                                events.append(json.loads(data))
                            except ValueError:
                                pass
    return 200, None, events


def summarize_stream(events):
    texts, thinks = [], []
    cur = None
    for ev in events:
        t = ev.get('type')
        if t == 'content_block_start':
            cur = (ev.get('content_block') or {}).get('type')
            if cur == 'text':
                texts.append('')
            elif cur == 'thinking':
                thinks.append('')
        elif t == 'content_block_delta':
            d = ev.get('delta') or {}
            if cur == 'text' and d.get('type') == 'text_delta':
                texts[-1] += d.get('text') or ''
            elif cur == 'thinking' and d.get('type') == 'thinking_delta':
                thinks[-1] += d.get('thinking') or ''
    return texts, thinks


def artifacts(texts, thinks):
    found = []
    for i, t in enumerate(texts):
        if CLOSE in t:
            found.append(f'text[{i}] contains literal closing tag: ...{t[-40:]!r}')
        if t.strip() == '' and t:
            found.append(f'text[{i}] is whitespace-only: {t!r}')
    for i, t in enumerate(thinks):
        if CLOSE in t:
            found.append(f'think[{i}] contains closing tag')
    return found


async def main():
    print('== TEST A: literal tag mention in question (reasoning integrity) ==')
    status, err, events = await collect_events([
        {'role': 'user', 'content':
         'What does the XML tag pair ' + chr(60) + 'think' + chr(62) + ' and ' + CLOSE +
         ' do in LLM pipelines? Answer in ONE short paragraph and literally include both tags in your explanation.'}
    ])
    texts, thinks = summarize_stream(events)
    print(f'   HTTP {status}, {len(texts)} text blocks, {len(thinks)} thinking blocks')
    for i, t in enumerate(texts):
        print(f'   text[{i}] ({len(t)}ch): {t[:90]!r}')
    for i, t in enumerate(thinks):
        print(f'   think[{i}] ({len(t)}ch): {t[:90]!r}')
    issues = artifacts(texts, thinks)
    # reasoning must not leak into text: text must NOT start mid-sentence
    if texts and texts[0].lstrip().startswith('in your explanation'):
        issues.append('REASONING LEAK at text start')
    print('   ARTIFACTS:', issues if issues else 'NONE')
    ok_a = not issues

    print('== TEST B: simple turn ==')
    status, err, events = await collect_events(
        [{'role': 'user', 'content': 'Reply with exactly: CLEAN-OK. Nothing else.'}], max_tokens=200)
    texts, thinks = summarize_stream(events)
    print(f'   HTTP {status}, text={texts!r}')
    issues = artifacts(texts, thinks)
    print('   ARTIFACTS:', issues if issues else 'NONE')
    ok_b = not issues and any('CLEAN-OK' in t for t in texts)

    print('== TEST D: agentic tool turn (prose + tool) ==')
    tools = [{
        'name': 'bash', 'description': 'Run a shell command',
        'input_schema': {'type': 'object',
                         'properties': {'command': {'type': 'string'}},
                         'required': ['command']},
    }]
    status, err, events = await collect_events(
        [{'role': 'user', 'content': 'Run `uname -s` with the bash tool.'}],
        max_tokens=600, tools=tools)
    texts, thinks = summarize_stream(events)
    tool_starts = sum(1 for ev in events
                      if ev.get('type') == 'content_block_start'
                      and (ev.get('content_block') or {}).get('type') == 'tool_use')
    print(f'   HTTP {status}, {len(texts)} text, {len(thinks)} think, {tool_starts} tool_use')
    for i, t in enumerate(texts):
        print(f'   text[{i}] ({len(t)}ch): {t[-60:]!r}')
    issues = artifacts(texts, thinks)
    print('   ARTIFACTS:', issues if issues else 'NONE')
    ok_d = not issues and tool_starts >= 1

    print('== TEST E: non-stream surface ==')
    status, data, _ = await collect_events(
        [{'role': 'user', 'content': 'What does the tag ' + CLOSE + ' mean? Mention it literally. One sentence.'}],
        stream=False, max_tokens=400)
    ok_e = False
    if status == 200 and isinstance(data, dict):
        blocks = data.get('content') or []
        for b in blocks:
            if b.get('type') == 'text' and CLOSE in (b.get('text') or ''):
                tail = (b.get('text') or '')[-50:]
                if tail.rstrip().endswith(CLOSE):
                    print(f'   NON-STREAM TRAILING TAG: {tail!r}')
                else:
                    print('   literal mention preserved mid-text (OK)')
            elif b.get('type') == 'text':
                print(f'   text block ({len(b.get("text") or "")}ch) clean tail: {(b.get("text") or "")[-40:]!r}')
        ok_e = not any((b.get('text') or '').rstrip().endswith(CLOSE)
                       for b in blocks if b.get('type') == 'text')
    else:
        print('   non-stream failed:', status, err)

    print()
    print('VERDICT:',
          'ALL CLEAN' if all([ok_a, ok_b, ok_d, ok_e]) else 'FAILURES PRESENT',
          {'A': ok_a, 'B': ok_b, 'D': ok_d, 'E': ok_e})


if __name__ == '__main__':
    asyncio.run(main())
