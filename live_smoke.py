#!/usr/bin/env python3
"""Live production smoke: drive every active wrapper on every surface.

Runs ON the VPS. Reads BEARER_TOKEN from each wrapper's .env (never prints it).
Checks protocol shapes end-to-end against real upstreams:
  /v1/messages      (Anthropic dialect, stream + non-stream, tools, thinking)
  /v1/chat/completions (OpenAI dialect, stream + non-stream, tools)
  /v1/responses     (Codex dialect, stream + non-stream)
Small max_tokens to keep cost minimal.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path('/root/wrapper')
WRAPPERS = {
    'nvidia-python': (9101, 'nvidia/nemotron-3-super-120b-a12b'),
    'nous': (9102, 'poolside/laguna-s-2.1:free'),
    'openrouter': (9106, 'nvidia/nemotron-3.5-lightning:free'),
}
RESULTS = []


def report(name, ok, detail):
    RESULTS.append((name, ok, detail))
    print(('PASS' if ok else 'FAIL') + f' | {name} | {detail[:180]}')


def token_for(name):
    env = (ROOT / name / '.env').read_text()
    m = re.search(r'^BEARER_TOKEN=(.+)$', env, re.M)
    return m.group(1).strip() if m else ''


def parse_sse(raw):
    events = []
    for block in re.split(r'\r?\n\r?\n', raw):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith('event:'):
                ev = line[6:].strip()
            elif line.startswith('data:'):
                d = line[5:].strip()
                if d and d != '[DONE]':
                    try:
                        data = json.loads(d)
                    except ValueError:
                        data = d
                elif d == '[DONE]':
                    data = '[DONE]'
        if ev is not None or data is not None:
            events.append((ev, data))
    return events


async def drive(session, url, headers, payload, timeout=90):
    t0 = time.time()
    try:
        async with session.post(url, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await r.text()
            return r.status, raw, time.time() - t0
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', time.time() - t0


async def smoke(name, port, model):
    base = f'http://127.0.0.1:{port}'
    headers = {'Authorization': f'Bearer {token_for(name)}',
               'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}
    async with aiohttp.ClientSession() as s:
        # ── 1. /v1/messages non-stream ──
        st, raw, ms = await drive(s, base + '/v1/messages', headers, {
            'model': model, 'max_tokens': 64,
            'messages': [{'role': 'user', 'content': 'Reply with exactly: WRAP-OK'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            d = json.loads(raw)
            ok = (d.get('type') == 'message' and d.get('role') == 'assistant'
                  and isinstance(d.get('content'), list) and d.get('stop_reason')
                  and str(d.get('id', '')).startswith('msg_')
                  and d.get('usage', {}).get('input_tokens', 0) >= 0)
            detail += f' stop={d.get("stop_reason")} id={d.get("id","")[:20]} blocks={len(d.get("content",[]))}'
        report(f'{name} messages nonstream', ok, detail)

        # ── 2. /v1/messages stream (lifecycle) ──
        st, raw, ms = await drive(s, base + '/v1/messages', headers, {
            'model': model, 'max_tokens': 64, 'stream': True,
            'messages': [{'role': 'user', 'content': 'Reply with exactly: WRAP-OK'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            evs = parse_sse(raw)
            types = [d.get('type') if isinstance(d, dict) else None for _e, d in evs]
            ok = (types.count('message_start') == 1 and types.count('message_stop') == 1
                  and 'content_block_start' in types and 'content_block_delta' in types
                  and 'message_delta' in types and 'ping' not in types
                  and '[DONE]' not in [d for _e, d in evs])
            detail += f' events={len(evs)} lifecycle={"ok" if ok else "broken"}'
        report(f'{name} messages stream', ok, detail)

        # ── 3. /v1/messages thinking (Nemotron Super controls path on nvidia) ──
        st, raw, ms = await drive(s, base + '/v1/messages', headers, {
            'model': model, 'max_tokens': 2048, 'stream': True,
            'thinking': {'type': 'enabled', 'budget_tokens': 512},
            'messages': [{'role': 'user', 'content': 'Is 17 prime? One sentence.'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            evs = parse_sse(raw)
            types = [d.get('type') if isinstance(d, dict) else None for _e, d in evs]
            thinking = any((d.get('content_block') or {}).get('type') == 'thinking'
                           for _e, d in evs if isinstance(d, dict))
            ok = types.count('message_stop') == 1
            detail += f' thinking_block={thinking} events={len(evs)}'
        report(f'{name} messages thinking+stream', ok, detail)

        # ── 4. /v1/messages tool round trip (Claude Code pattern) ──
        st, raw, ms = await drive(s, base + '/v1/messages', headers, {
            'model': model, 'max_tokens': 512,
            'tools': [{'name': 'get_weather', 'description': 'weather lookup',
                       'input_schema': {'type': 'object', 'properties': {
                           'city': {'type': 'string'}}, 'required': ['city']}}],
            'messages': [{'role': 'user', 'content': 'Use get_weather for Jakarta.'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            d = json.loads(raw)
            blocks = d.get('content', [])
            tu = [b for b in blocks if b.get('type') == 'tool_use']
            ok = bool(tu) and d.get('stop_reason') == 'tool_use'
            if tu:
                detail += f' tool={tu[0].get("name")} id={tu[0].get("id","")[:16]} stop={d.get("stop_reason")}'
        report(f'{name} messages tools', ok, detail)

        # ── 5. /v1/chat/completions non-stream (OpenAI native / Hermes / Openclaw) ──
        # Native dialect passthrough: upstream id (chatcmpl- or gen-) preserved.
        st, raw, ms = await drive(s, base + '/v1/chat/completions', headers, {
            'model': model, 'max_tokens': 48,
            'messages': [{'role': 'user', 'content': 'Reply with exactly: WRAP-OK'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            d = json.loads(raw)
            ok = (d.get('object') == 'chat.completion' and d.get('id')
                  and d.get('choices') and d['choices'][0].get('message'))
            detail += f' id={d.get("id","")[:22]} finish={d.get("choices",[{}])[0].get("finish_reason")}'
        report(f'{name} chat nonstream', ok, detail)

        # ── 6. /v1/chat/completions stream + tools ──
        st, raw, ms = await drive(s, base + '/v1/chat/completions', headers, {
            'model': model, 'max_tokens': 256, 'stream': True,
            'tools': [{'type': 'function', 'function': {'name': 'get_weather',
                       'description': 'weather', 'parameters': {'type': 'object',
                       'properties': {'city': {'type': 'string'}}}}}],
            'messages': [{'role': 'user', 'content': 'Call get_weather for Jakarta.'}]})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            evs = parse_sse(raw)
            payloads = [d for _e, d in evs]
            done_ok = payloads and payloads[-1] == '[DONE]'
            finish_seen = any(isinstance(d, dict) and any(
                c.get('finish_reason') is not None for c in d.get('choices', []))
                for d in payloads)
            any_tool = any(isinstance(d, dict) and any(
                tc.get('function') for tc in
                ((c.get('delta') or {}).get('tool_calls') or []))
                for d in payloads if isinstance(d, dict) for c in d.get('choices', []))
            # Terminal signal: [DONE] OR a finish_reason chunk (Nous upstream
            # legitimately omits [DONE] after the usage frame; transparent
            # passthrough must not synthesize one).
            ok = done_ok or finish_seen
            detail += f' terminal={"[DONE]" if done_ok else ("finish_reason" if finish_seen else "NONE")} tool_calls={"yes" if any_tool else "no"} frames={len(evs)}'
        report(f'{name} chat stream+tools', ok, detail)

        # ── 7. /v1/responses non-stream (Codex pattern; native passthrough or
        # translated depending on upstream Responses support) ──
        st, raw, ms = await drive(s, base + '/v1/responses', headers, {
            'model': model, 'max_output_tokens': 64, 'input': 'Reply with exactly: WRAP-OK'})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            d = json.loads(raw)
            ok = (d.get('object') == 'response' and d.get('id')
                  and d.get('status') in ('completed', 'incomplete', 'failed')
                  and isinstance(d.get('output'), list))
            detail += f' status={d.get("status")} id={d.get("id","")[:20]} items={len(d.get("output") or [])}'
        report(f'{name} responses nonstream', ok, detail)

        # ── 8. /v1/responses stream ──
        st, raw, ms = await drive(s, base + '/v1/responses', headers, {
            'model': model, 'max_output_tokens': 64, 'stream': True,
            'input': 'Reply with exactly: WRAP-OK'})
        ok = st == 200
        detail = f'HTTP {st} {ms:.1f}s'
        if ok:
            evs = parse_sse(raw)
            types = [d.get('type') for _e, d in evs if isinstance(d, dict)]
            terminal = [t for t in types if t in ('response.completed', 'response.failed', 'response.incomplete')]
            ok = (types and types[0] == 'response.created' and bool(terminal))
            detail += f' events={len(evs)} terminal={terminal[-1] if terminal else types[-3:]}'
        report(f'{name} responses stream', ok, detail)


async def main():
    print(f'live smoke against real upstreams @ {time.strftime("%F %T")}')
    for name, (port, model) in WRAPPERS.items():
        print(f'── {name} ({model}) ──')
        try:
            await smoke(name, port, model)
        except Exception as e:
            report(name, False, f'harness error {type(e).__name__}: {e}')
    fails = [r for r in RESULTS if not r[1]]
    print(f'\nTOTAL: {len(RESULTS)} checks, {len(RESULTS) - len(fails)} pass, {len(fails)} fail')
    for name, _ok, detail in fails:
        print(f'  FAIL {name}: {detail}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
