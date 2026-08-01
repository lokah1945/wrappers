#!/usr/bin/env python3
"""Runtime E2E harness — boots every wrapper as a REAL uvicorn server against a
mock upstream and drives it exactly like Claude Code / Codex / an OpenAI SDK
client would.

This exists because unit tests import functions; agents open sockets. Every
bug found by this harness was invisible to the 110-test suite.

Usage:  python tests/e2e_runtime/run_runtime_e2e.py [--wrapper NAME] [-v]
Exit code 0 = zero runtime errors across every wrapper x surface x mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
MOCK_PORT = 19999
TOKEN = 'runtime-e2e-token'

WRAPPERS = {
    # name: (dir, port, upstream_env_var, extra_env)
    'nvidia-python': ('nvidia-python', 19101, 'NVIDIA_BASE_URL', {}),
    'nous':          ('nous',          19102, 'NOUS_BASE_URL',   {}),
    'opencode':      ('opencode',      19103, 'OPENCODE_BASE_URL', {}),
    'blackbox':      ('blackbox',      19104, 'BLACKBOX_BASE_URL', {}),
    'openrouter':    ('openrouter',    19106, 'OPENROUTER_BASE_URL', {}),
}

# nvidia-python and nous append '/v1' to the configured base themselves;
# opencode/blackbox/openrouter expect the base to already include it.
BASE_FOR = {
    'nvidia-python': f'http://127.0.0.1:{MOCK_PORT}',
    'nous':          f'http://127.0.0.1:{MOCK_PORT}',
    'opencode':      f'http://127.0.0.1:{MOCK_PORT}/v1',
    'blackbox':      f'http://127.0.0.1:{MOCK_PORT}/v1',
    'openrouter':    f'http://127.0.0.1:{MOCK_PORT}/v1',
}

STREAM_MODES = [
    'normal', 'nospace', 'keepalive', 'crlf', 'tools', 'reasoning', 'reasoning_only',
    'nofinish', 'noterminator', 'midstream_error', 'usage_after', 'empty', 'unicode',
    'slow',
    # Round-2 adversarial framing / protocol modes
    'bigchunk', 'bytesplit', 'comments', 'dupfinish', 'nullcontent',
    'emptychoices', 'toolnoid', 'longtool',
]

# Upstream returns an HTTP error before any streaming: the wrapper must return a
# shaped 4xx/5xx envelope, never hang and never 200-with-empty-stream.
ERROR_MODES = ['http500', 'http429']

FAILURES: list[str] = []
CHECKS = [0]
VERBOSE = False


def log(msg: str):
    pass


def _log_unused(msg: str):
    if VERBOSE:
        print(f'    {msg}')


def fail(wrapper: str, surface: str, mode: str, msg: str):
    entry = f'[{wrapper}] {surface} mode={mode}: {msg}'
    FAILURES.append(entry)
    print(f'  ✗ {entry}', flush=True)


def ok(wrapper: str, surface: str, mode: str, note: str = ''):
    CHECKS[0] += 1
    if VERBOSE:
        print(f'  ✓ [{wrapper}] {surface} mode={mode} {note}', flush=True)


def health_wait(port: int, timeout: float = 90.0) -> bool:
    """An open port is not readiness: nvidia runs a model-verification sweep at
    startup, so poll /health until it actually answers."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/health')
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def free_port_wait(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ── SSE parsing ────────────────────────────────────────────────────────────

def parse_sse(raw: str) -> list[tuple[str | None, object]]:
    """Parse an SSE body into (event, data) pairs. Raises on malformed frames."""
    events = []
    raw = raw.replace('\r\n', '\n')  # CRLF framing is legal SSE
    for block in raw.split('\n\n'):
        block = block.strip('\n')
        if not block.strip():
            continue
        ev = None
        datas = []
        for line in block.split('\n'):
            if line.startswith(':'):
                continue  # comment / heartbeat
            if line.startswith('event:'):
                ev = line[6:].strip()
            elif line.startswith('data:'):
                v = line[5:].strip()
                if v:            # bare `data:` = legal empty keep-alive event
                    datas.append(v)
            elif line.split(':', 1)[0] in ('id', 'retry', 'event', 'data'):
                pass  # other legal SSE fields (WHATWG 9.2.6) — ignored
            elif line.strip():
                raise ValueError(f'malformed SSE line: {line!r}')
        if not datas:
            continue
        payload = '\n'.join(datas)
        if payload == '[DONE]':
            events.append((ev, '[DONE]'))
            continue
        try:
            events.append((ev, json.loads(payload)))
        except json.JSONDecodeError as e:
            raise ValueError(f'non-JSON SSE data: {payload[:200]!r} ({e})')
    return events


def check_anthropic_stream(evs, mode) -> list[str]:
    """Validate the Anthropic Messages SSE contract as Claude Code enforces it."""
    errs = []
    types = [d.get('type') for _e, d in evs if isinstance(d, dict)]
    if not types:
        return ['no events emitted']
    if types[0] != 'message_start':
        errs.append(f'first event is {types[0]!r}, expected message_start')
    if 'message_stop' not in types:
        errs.append('missing terminal message_stop (client hangs)')
    else:
        if types[-1] != 'message_stop':
            errs.append(f'last event is {types[-1]!r}, expected message_stop')
        if types.count('message_stop') != 1:
            errs.append(f'{types.count("message_stop")} message_stop events (must be exactly 1)')
    if types.count('message_start') != 1:
        errs.append(f'{types.count("message_start")} message_start events (must be exactly 1)')

    # Block lifecycle: every start must be matched by a stop on the same index,
    # and indices must never be reused while open.
    open_idx = {}
    seen_idx = set()
    for _e, d in evs:
        if not isinstance(d, dict):
            continue
        t = d.get('type')
        if t == 'content_block_start':
            i = d.get('index')
            if i in open_idx:
                errs.append(f'content_block_start on already-open index {i}')
            if i in seen_idx:
                errs.append(f'content_block index {i} reused after close')
            cb = d.get('content_block') or {}
            if cb.get('type') not in ('text', 'thinking', 'tool_use'):
                errs.append(f'unknown content_block type {cb.get("type")!r}')
            if cb.get('type') == 'tool_use' and not cb.get('name'):
                errs.append(f'tool_use block at index {i} has empty name (phantom block)')
            open_idx[i] = cb.get('type')
        elif t == 'content_block_delta':
            i = d.get('index')
            if i not in open_idx:
                errs.append(f'content_block_delta on unopened index {i}')
        elif t == 'content_block_stop':
            i = d.get('index')
            if i not in open_idx:
                errs.append(f'content_block_stop on unopened index {i}')
            else:
                seen_idx.add(i)
                open_idx.pop(i, None)
    if open_idx:
        errs.append(f'unclosed content blocks at exit: {sorted(open_idx)}')

    # No events may follow message_stop.
    if 'message_stop' in types:
        after = types[types.index('message_stop') + 1:]
        if after:
            errs.append(f'events emitted AFTER message_stop: {after}')

    # Protocol frames must never be rendered as assistant text (B-10 class).
    for _e, d in evs:
        if isinstance(d, dict) and d.get('type') == 'content_block_delta':
            txt = (d.get('delta') or {}).get('text') or ''
            if 'event:' in txt or 'data: {' in txt or 'content_block_stop' in txt:
                errs.append(f'SSE protocol frame leaked into text_delta: {txt[:80]!r}')

    if mode == 'tools':
        starts = [d for _e, d in evs if isinstance(d, dict)
                  and d.get('type') == 'content_block_start'
                  and (d.get('content_block') or {}).get('type') == 'tool_use']
        if len(starts) != 2:
            errs.append(f'{len(starts)} tool_use blocks, expected 2 (parallel tools)')
        if len({s.get('index') for s in starts}) != len(starts):
            errs.append('parallel tool blocks collided on the same index')
        args = {}
        for _e, d in evs:
            if isinstance(d, dict) and d.get('type') == 'content_block_delta':
                dl = d.get('delta') or {}
                if dl.get('type') == 'input_json_delta':
                    args[d['index']] = args.get(d['index'], '') + dl.get('partial_json', '')
        for i, blob in args.items():
            try:
                json.loads(blob)
            except json.JSONDecodeError:
                errs.append(f'tool block {i} arguments are not valid JSON: {blob!r}')
        if len(args) != 2:
            errs.append(f'{len(args)} tools received arguments, expected 2')

    if mode == 'midstream_error':
        has_err = any(isinstance(d, dict) and d.get('type') == 'error' for _e, d in evs)
        stops = [d for _e, d in evs if isinstance(d, dict) and d.get('type') == 'message_delta']
        if not has_err:
            sr = stops[0].get('delta', {}).get('stop_reason') if stops else None
            errs.append(f'upstream error reported as success (stop_reason={sr!r}); '
                        'client cannot detect the failure or retry')
    return errs


def check_openai_stream(evs, mode) -> list[str]:
    errs = []
    if not evs:
        return ['no events emitted']
    payloads = [d for _e, d in evs]
    if '[DONE]' not in payloads:
        errs.append('missing terminal data: [DONE] (client hangs)')
    elif payloads[-1] != '[DONE]':
        errs.append('data: [DONE] is not the final frame')
    for d in payloads:
        if d == '[DONE]':
            continue
        if not isinstance(d, dict):
            errs.append(f'non-object SSE payload: {d!r}')
            continue
        if 'error' in d:
            continue  # a surfaced upstream error is legitimate
        if d.get('object') not in ('chat.completion.chunk', None):
            errs.append(f'unexpected object {d.get("object")!r} on chat surface')
        for ch in d.get('choices') or []:
            delta = ch.get('delta')
            if delta is None and ch.get('finish_reason') is None:
                errs.append('choice with neither delta nor finish_reason')
    if mode == 'tools':
        names, args = set(), {}
        for d in payloads:
            if not isinstance(d, dict):
                continue
            for ch in d.get('choices') or []:
                for tc in (ch.get('delta') or {}).get('tool_calls') or []:
                    fn = tc.get('function') or {}
                    if fn.get('name'):
                        names.add(fn['name'])
                    if fn.get('arguments'):
                        args[tc.get('index', 0)] = args.get(tc.get('index', 0), '') + fn['arguments']
        if len(args) != 2:
            errs.append(f'{len(args)} tool argument streams, expected 2')
        for i, blob in args.items():
            try:
                json.loads(blob)
            except json.JSONDecodeError:
                errs.append(f'tool {i} arguments not valid JSON: {blob!r}')
    return errs


def check_responses_stream(evs, mode) -> list[str]:
    errs = []
    types = [d.get('type') for _e, d in evs if isinstance(d, dict)]
    if not types:
        return ['no events emitted']
    if types[0] != 'response.created':
        errs.append(f'first event {types[0]!r}, expected response.created')
    terminal = [t for t in types if t in ('response.completed', 'response.failed',
                                          'response.incomplete')]
    if not terminal:
        errs.append('no terminal response.completed/failed event (Codex hangs)')
    if mode == 'midstream_error' and 'response.completed' in types:
        errs.append('upstream error reported as response.completed')
    # CODEX-RESP-01 guard: a *completed* turn must have a complete item
    # lifecycle — every output_item.added must be matched by an
    # output_item.done. The openrouter translator used to skip the done
    # events when the model emitted only reasoning (no text), so Codex never
    # saw its output items close and hung waiting for the terminal events.
    # Failure paths (response.failed) are exempt: siblings close items only
    # on success, matching the OpenAI Responses error shape.
    if 'response.completed' in types:
        added = {(d.get('output_index'), (d.get('item') or {}).get('id'))
                 for _e, d in evs
                 if isinstance(d, dict) and d.get('type') == 'response.output_item.added'}
        done = {(d.get('output_index'), (d.get('item') or {}).get('id'))
                for _e, d in evs
                if isinstance(d, dict) and d.get('type') == 'response.output_item.done'}
        if not added:
            errs.append('no response.output_item.added before response.completed '
                        '(Codex has no active item — hangs)')
        for idx, iid in sorted(added):
            if (idx, iid) not in done:
                errs.append(f'output item {iid} (index {idx}) added but never done '
                            '(Codex hangs waiting for item close)')
    # Sequence numbers, when present, must be strictly increasing.
    seqs = [d['sequence_number'] for _e, d in evs
            if isinstance(d, dict) and isinstance(d.get('sequence_number'), int)]
    if seqs and any(b <= a for a, b in zip(seqs, seqs[1:])):
        errs.append('sequence_number is not strictly increasing')
    return errs


# ── request drivers ────────────────────────────────────────────────────────

async def drive_stream(session, url, payload, headers):
    """Return (status, raw_body, error). Never raises.

    On timeout, report how many bytes/events DID arrive so a hang can be
    localized to a specific point in the event lifecycle.
    """
    buf = []
    try:
        async with session.post(url, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=45)) as r:
            async for part in r.content.iter_any():
                buf.append(part)
            return r.status, b''.join(buf).decode('utf-8', 'replace'), None
    except Exception as e:
        got = b''.join(buf).decode('utf-8', 'replace')
        tailinfo = ''
        if got:
            evs = [l for l in got.split('\n') if l.startswith('event:')]
            tailinfo = f' [received {len(got)}B, last events: {evs[-3:]}]'
        return 0, got, f'{type(e).__name__}: {e}{tailinfo}'


async def exercise(wrapper: str, port: int) -> None:
    base = f'http://127.0.0.1:{port}'
    headers = {'Authorization': f'Bearer {TOKEN}', 'anthropic-version': '2023-06-01'}
    # A real agent keeps ONE session open across many turns. Bound the pool so
    # an abandoned/leaked upstream stream shows up as a client-visible stall
    # (which is exactly what we want to detect) rather than being masked by an
    # unbounded connector.
    _conn = aiohttp.TCPConnector(limit=32, force_close=False)
    async with aiohttp.ClientSession(connector=_conn) as s:
        # ── discovery surfaces an agent hits on startup ──
        for path in ('/health', '/ready', '/v1/models', '/api/tags', '/metrics'):
            try:
                async with s.get(base + path, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    body = await r.text()
                    if r.status >= 500:
                        fail(wrapper, path, '-', f'HTTP {r.status}: {body[:200]}')
                    else:
                        ok(wrapper, path, '-', f'{r.status}')
            except Exception as e:
                fail(wrapper, path, '-', f'{type(e).__name__}: {e}')

        # ── /v1/messages (Claude Code) ──
        for mode in STREAM_MODES:
            model = f'mock/{mode}'
            payload = {'model': model, 'max_tokens': 256, 'stream': True,
                       'messages': [{'role': 'user', 'content': 'hi'}]}
            if mode == 'tools':
                payload['tools'] = [
                    {'name': 'alpha', 'description': 'a',
                     'input_schema': {'type': 'object', 'properties': {'x': {'type': 'number'}}}},
                    {'name': 'beta', 'description': 'b',
                     'input_schema': {'type': 'object', 'properties': {'y': {'type': 'number'}}}}]
            st, raw, err = await drive_stream(s, base + '/v1/messages', payload, headers)
            if err:
                fail(wrapper, '/v1/messages', mode, f'transport: {err}')
                continue
            if st != 200:
                fail(wrapper, '/v1/messages', mode, f'HTTP {st}: {raw[:200]}')
                continue
            try:
                evs = parse_sse(raw)
            except ValueError as e:
                fail(wrapper, '/v1/messages', mode, f'unparsable SSE: {e}')
                continue
            errs = check_anthropic_stream(evs, mode)
            if errs:
                for e in errs:
                    fail(wrapper, '/v1/messages', mode, e)
            else:
                ok(wrapper, '/v1/messages', mode, f'{len(evs)} events')

        # ── /v1/chat/completions (OpenAI SDK) ──
        for mode in STREAM_MODES:
            payload = {'model': f'mock/{mode}', 'stream': True,
                       'messages': [{'role': 'user', 'content': 'hi'}]}
            if mode == 'tools':
                payload['tools'] = [
                    {'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}},
                    {'type': 'function', 'function': {'name': 'beta', 'parameters': {'type': 'object'}}}]
            st, raw, err = await drive_stream(s, base + '/v1/chat/completions', payload, headers)
            if err:
                fail(wrapper, '/v1/chat/completions', mode, f'transport: {err}')
                continue
            if st != 200:
                fail(wrapper, '/v1/chat/completions', mode, f'HTTP {st}: {raw[:200]}')
                continue
            try:
                evs = parse_sse(raw)
            except ValueError as e:
                fail(wrapper, '/v1/chat/completions', mode, f'unparsable SSE: {e}')
                continue
            errs = check_openai_stream(evs, mode)
            if errs:
                for e in errs:
                    fail(wrapper, '/v1/chat/completions', mode, e)
            else:
                ok(wrapper, '/v1/chat/completions', mode, f'{len(evs)} events')

        # ── /v1/responses (Codex) ──
        for mode in STREAM_MODES:
            payload = {'model': f'mock/{mode}', 'stream': True, 'input': 'hi'}
            _t0 = time.time()
            st, raw, err = await drive_stream(s, base + '/v1/responses', payload, headers)
            if err:
                # Capture pool state so a transport timeout can be attributed to
                # key starvation vs a genuine generator hang.
                _pool = '?'
                try:
                    async with s.get(base + '/health', headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as _hr:
                        _hj = await _hr.json()
                        _pool = f"available={_hj.get('available')} keys={_hj.get('keys')}"
                    try:
                        async with s.get(base + '/_debug/conn', headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=10)) as _cr:
                            _pool += ' conn=' + json.dumps(await _cr.json())
                    except Exception:
                        pass
                except Exception as _pe:
                    _pool = f'health-unreachable: {type(_pe).__name__}'
                fail(wrapper, '/v1/responses', mode,
                     f'transport: {err} after {time.time()-_t0:.1f}s; pool[{_pool}]')
                continue
            if st == 404:
                continue  # surface not offered by this wrapper
            if st != 200:
                fail(wrapper, '/v1/responses', mode, f'HTTP {st}: {raw[:200]}')
                continue
            try:
                evs = parse_sse(raw)
            except ValueError as e:
                fail(wrapper, '/v1/responses', mode, f'unparsable SSE: {e}')
                continue
            errs = check_responses_stream(evs, mode)
            if errs:
                for e in errs:
                    fail(wrapper, '/v1/responses', mode, e)
            else:
                ok(wrapper, '/v1/responses', mode, f'{len(evs)} events')

        # ── upstream HTTP errors must surface as shaped errors, not hangs ──
        for mode in ERROR_MODES:
            for path, payload in (
                ('/v1/chat/completions',
                 {'model': f'mock/{mode}', 'stream': True,
                  'messages': [{'role': 'user', 'content': 'hi'}]}),
                ('/v1/messages',
                 {'model': f'mock/{mode}', 'max_tokens': 64, 'stream': True,
                  'messages': [{'role': 'user', 'content': 'hi'}]}),
            ):
                st, raw, err = await drive_stream(s, base + path, payload, headers)
                if err:
                    fail(wrapper, path, mode, f'transport: {err}')
                    continue
                if st == 200:
                    # A 200 is only acceptable if the body carries a terminal
                    # error event; otherwise the client sees a silent empty turn.
                    if 'error' not in raw and 'message_stop' not in raw and '[DONE]' not in raw:
                        fail(wrapper, path, mode,
                             'HTTP 200 with no error and no terminator (client hangs)')
                    else:
                        ok(wrapper, path, mode, '200+terminal')
                elif 400 <= st < 600:
                    try:
                        d = json.loads(raw)
                        if not isinstance(d, dict) or 'error' not in d:
                            fail(wrapper, path, mode, f'HTTP {st} body is not a shaped error: {raw[:150]}')
                        else:
                            ok(wrapper, path, mode, f'{st} shaped')
                    except json.JSONDecodeError:
                        fail(wrapper, path, mode, f'HTTP {st} body is not JSON: {raw[:150]}')
                else:
                    fail(wrapper, path, mode, f'unexpected status {st}')

        # ── non-streaming JSON on all three surfaces ──
        for path, payload, shape in (
            ('/v1/chat/completions',
             {'model': 'mock/normal', 'messages': [{'role': 'user', 'content': 'hi'}]}, 'openai'),
            ('/v1/messages',
             {'model': 'mock/normal', 'max_tokens': 64,
              'messages': [{'role': 'user', 'content': 'hi'}]}, 'anthropic'),
            ('/v1/responses', {'model': 'mock/normal', 'input': 'hi'}, 'responses'),
        ):
            try:
                async with s.post(base + path, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                    body = await r.text()
                    if r.status == 404:
                        continue
                    if r.status != 200:
                        fail(wrapper, path, 'nonstream', f'HTTP {r.status}: {body[:200]}')
                        continue
                    d = json.loads(body)
                    if shape == 'openai':
                        if not d.get('choices'):
                            fail(wrapper, path, 'nonstream', 'no choices in response')
                        elif d['choices'][0].get('message', {}).get('content') is None:
                            fail(wrapper, path, 'nonstream', 'message.content is null (SDK crash)')
                        else:
                            ok(wrapper, path, 'nonstream')
                    elif shape == 'anthropic':
                        if d.get('type') != 'message':
                            fail(wrapper, path, 'nonstream', f'type={d.get("type")!r}, expected message')
                        elif not isinstance(d.get('content'), list) or not d['content']:
                            fail(wrapper, path, 'nonstream', 'content must be a non-empty array')
                        elif not d.get('stop_reason'):
                            fail(wrapper, path, 'nonstream', 'missing stop_reason')
                        else:
                            ok(wrapper, path, 'nonstream')
                    else:
                        if d.get('object') != 'response':
                            fail(wrapper, path, 'nonstream', f'object={d.get("object")!r}')
                        elif not d.get('output'):
                            fail(wrapper, path, 'nonstream', 'no output items')
                        else:
                            ok(wrapper, path, 'nonstream')
            except Exception as e:
                fail(wrapper, path, 'nonstream', f'{type(e).__name__}: {e}')

        # ── multi-turn tool round trip (the real agent loop) ──
        try:
            conv = {'model': 'mock/tools', 'max_tokens': 256,
                    'messages': [
                        {'role': 'user', 'content': 'call both tools'},
                        {'role': 'assistant', 'content': [
                            {'type': 'tool_use', 'id': 'call_a', 'name': 'alpha', 'input': {'x': 1}},
                            {'type': 'tool_use', 'id': 'call_b', 'name': 'beta', 'input': {'y': 2}}]},
                        {'role': 'user', 'content': [
                            {'type': 'tool_result', 'tool_use_id': 'call_a', 'content': 'ok-a'},
                            {'type': 'tool_result', 'tool_use_id': 'call_b', 'content': 'ok-b'}]},
                    ],
                    'tools': [{'name': 'alpha', 'input_schema': {'type': 'object'}},
                              {'name': 'beta', 'input_schema': {'type': 'object'}}]}
            async with s.post(base + '/v1/messages', json=conv, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.text()
                if r.status != 200:
                    fail(wrapper, '/v1/messages', 'toolresult', f'HTTP {r.status}: {body[:250]}')
                else:
                    ok(wrapper, '/v1/messages', 'toolresult')
        except Exception as e:
            fail(wrapper, '/v1/messages', 'toolresult', f'{type(e).__name__}: {e}')

        # ── client disconnect mid-stream (must not wedge the pool) ──
        try:
            timeout = aiohttp.ClientTimeout(total=0.35)
            try:
                async with s.post(base + '/v1/messages',
                                  json={'model': 'mock/slow', 'max_tokens': 64, 'stream': True,
                                        'messages': [{'role': 'user', 'content': 'hi'}]},
                                  headers=headers, timeout=timeout) as r:
                    await r.content.read(10)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                pass
            await asyncio.sleep(1.5)
            async with s.get(base + '/health', headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                h = await r.json()
                avail = h.get('available', h.get('available_keys'))
                if avail is not None and avail == 0:
                    fail(wrapper, 'disconnect', 'leak',
                         f'key pool starved after client disconnect (available={avail})')
                else:
                    ok(wrapper, 'disconnect', 'leak', f'available={avail}')
        except Exception as e:
            fail(wrapper, 'disconnect', 'leak', f'{type(e).__name__}: {e}')

        # ── concurrency: 12 simultaneous streams ──
        try:
            tasks = [drive_stream(s, base + '/v1/messages',
                                  {'model': 'mock/normal', 'max_tokens': 64, 'stream': True,
                                   'messages': [{'role': 'user', 'content': f'q{i}'}]}, headers)
                     for i in range(12)]
            results = await asyncio.gather(*tasks)
            bad = [r for r in results if r[2] or r[0] != 200]
            if bad:
                fail(wrapper, 'concurrency', '12x', f'{len(bad)}/12 failed: {bad[0][2] or bad[0][0]}')
            else:
                ok(wrapper, 'concurrency', '12x')
        except Exception as e:
            fail(wrapper, 'concurrency', '12x', f'{type(e).__name__}: {e}')

        # ── malformed input must yield 4xx, never 5xx ──
        for name, raw_body in (
            ('badjson', b'{not json'),
            ('empty', b''),
            ('nonobject', b'[1,2,3]'),
        ):
            for path in ('/v1/chat/completions', '/v1/messages'):
                try:
                    async with s.post(base + path, data=raw_body,
                                      headers={**headers, 'Content-Type': 'application/json'},
                                      timeout=aiohttp.ClientTimeout(total=15)) as r:
                        body = await r.text()
                        if r.status >= 500:
                            fail(wrapper, path, name, f'HTTP {r.status} on malformed input: {body[:150]}')
                        else:
                            ok(wrapper, path, name, f'{r.status}')
                except Exception as e:
                    fail(wrapper, path, name, f'{type(e).__name__}: {e}')


# ── process management ─────────────────────────────────────────────────────

def start_wrapper(name: str, wdir: str, port: int, upstream_var: str, extra: dict):
    env = os.environ.copy()
    env.update({
        'WRAPPER_SKIP_DOTENV': 'true',
        'BEARER_TOKEN': TOKEN,
        'LISTEN_PORT': str(port),
        'LISTEN_HOST': '127.0.0.1',
        'RATE_LIMIT_RPM': '0',
        'HEARTBEAT_INTERVAL_MS': '300',
        upstream_var: BASE_FOR[name],
        'PYTHONPATH': f'{ROOT}:{ROOT / wdir}',
        'PYTHONUNBUFFERED': '1',
        'FREE_ONLY': 'no',
        'MODEL_REGISTRY_STRICT': 'false',
        # HARNESS NOTE: this suite fires ~55 requests in well under a minute.
        # nvidia's key pool defaults to SOFT_LIMIT_RPM=30 and deliberately
        # PACES admission above that — correct production behaviour, but it
        # looks like a hang to a 45s client timeout. Raise the per-key limits so
        # the harness measures protocol correctness, not rate limiting.
        'SOFT_LIMIT_RPM': '100000',
        'HARD_LIMIT_RPM': '100000',
        'NVIDIA_SOFT_LIMIT_RPM': '100000',
        'NVIDIA_HARD_LIMIT_RPM': '100000',
        'NOUS_HARD_LIMIT_RPM': '100000',
        'OPENCODE_HARD_LIMIT_RPM': '100000',
        'BLACKBOX_HARD_LIMIT_RPM': '100000',
        'OPENROUTER_HARD_LIMIT_RPM': '100000',
    })
    # per-wrapper key pool env
    for pfx in ('NVIDIA', 'NOUS', 'OPENCODE', 'BLACKBOX', 'OPENROUTER'):
        env[f'{pfx}_API_KEY_1'] = 'mock-key-0000000001'
        env[f'{pfx}_API_KEY_2'] = 'mock-key-0000000002'
        env[f'{pfx}_BASE_URL'] = BASE_FOR[name]
    env.update(extra)

    logf = open(f'/tmp/rt-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / wdir), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def scan_log(name: str) -> list[str]:
    """Surface tracebacks / ERROR lines from a wrapper's stderr."""
    path = f'/tmp/rt-{name}.log'
    if not os.path.exists(path):
        return []
    txt = open(path, errors='replace').read()
    hits = []
    # Filter benign optional-telemetry noise (no Loki/Prometheus in the sandbox).
    txt = '\n'.join(l for l in txt.split('\n')
                    if 'loki_push' not in l and ':3100' not in l)
    if 'Traceback (most recent call last)' in txt:
        for blk in txt.split('Traceback (most recent call last)')[1:]:
            hits.append('Traceback:' + blk.strip().split('\n')[-1][:200])
    for marker in ('async generator ignored GeneratorExit',
                   'Task exception was never retrieved',
                   'RuntimeError',
                   'was never awaited',
                   'Unclosed client session',
                   'Unclosed connector'):
        if marker in txt:
            hits.append(f'log marker: {marker}')
    return hits


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrapper')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    VERBOSE = args.verbose

    targets = ({args.wrapper: WRAPPERS[args.wrapper]} if args.wrapper else WRAPPERS)

    mock = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(MOCK_PORT)],
        stdout=open('/tmp/rt-mock.log', 'w'), stderr=subprocess.STDOUT)
    if not free_port_wait(MOCK_PORT):
        print('FATAL: mock upstream did not start'); mock.kill(); return 2
    print(f'mock upstream up on :{MOCK_PORT}\n')

    try:
        for name, (wdir, port, upstream_var, extra) in targets.items():
            print(f'── {name} ' + '─' * (58 - len(name)))
            proc, logf = start_wrapper(name, wdir, port, upstream_var, extra)
            try:
                if not free_port_wait(port) or not health_wait(port):
                    tail = open(f'/tmp/rt-{name}.log', errors='replace').read()[-1500:]
                    fail(name, 'boot', '-', f'did not become healthy.\n{tail}')
                    continue
                asyncio.run(exercise(name, port))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                logf.close()
            for hit in scan_log(name):
                fail(name, 'server-log', '-', hit)
            print()
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    print('=' * 68)
    print(f'checks passed: {CHECKS[0]}    failures: {len(FAILURES)}')
    print('=' * 68)
    if FAILURES:
        print('\nFAILURES:')
        for f in FAILURES:
            print('  ✗ ' + f)
        return 1
    print('\n✅ zero runtime errors across all wrappers × surfaces × modes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
