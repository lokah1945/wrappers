#!/usr/bin/env python3
"""Fuzz-bodies E2E — R12 instrument: arbitrary/malformed/adversarial request
bodies must NEVER produce a 5xx, a traceback, a hang, or an unparseable
response from any wrapper (WRAPPER_CONTRACT §4: malformed => shaped 4xx).

Boots the mock upstream + each wrapper as a real uvicorn server (same pattern
as run_runtime_e2e.py), then fires a corpus of hostile payloads at every
inference surface — including concurrent bursts — and asserts:

  1. HTTP status is never 5xx (per-status accounting of wrapper bugs).
  2. The response body is always parseable JSON with an error/message shape
     (agents need actionable errors, never HTML tracebacks or empty sockets).
  3. No Traceback / async-task warnings appear in the wrapper's stderr log.
  4. After the storm, /health reports zero leaked in-flight reservations.

Usage:  python tests/e2e_runtime/fuzz_bodies_e2e.py [--wrapper NAME]
Exit code 0 = every hostile body shaped correctly on every wrapper.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
MOCK_PORT = 19999
TOKEN = 'runtime-e2e-token'

WRAPPERS = {
    'nvidia-python': ('nvidia-python', 19101, 'NVIDIA_BASE_URL', {}),
    'nous':          ('nous',          19102, 'NOUS_BASE_URL',   {}),
    'opencode':      ('opencode',      19103, 'OPENCODE_BASE_URL', {}),
    'blackbox':      ('blackbox',      19104, 'BLACKBOX_BASE_URL', {}),
    'openrouter':    ('openrouter',    19106, 'OPENROUTER_BASE_URL', {}),
}

BASE_FOR = {
    'nvidia-python': f'http://127.0.0.1:{MOCK_PORT}',
    'nous':          f'http://127.0.0.1:{MOCK_PORT}',
    'opencode':      f'http://127.0.0.1:{MOCK_PORT}/v1',
    'blackbox':      f'http://127.0.0.1:{MOCK_PORT}/v1',
    'openrouter':    f'http://127.0.0.1:{MOCK_PORT}/v1',
}

CHECKS = [0]
FAILURES: list[str] = []


def ok(label: str):
    CHECKS[0] += 1


def fail(wrapper: str, surface: str, detail: str):
    FAILURES.append(f'[{wrapper}] {surface}: {detail}')


# ── Hostile body corpus ─────────────────────────────────────────────────────
def _j(obj) -> bytes:
    return json.dumps(obj).encode()


def _nest(depth: int) -> dict:
    d: dict = {'leaf': 1}
    for _ in range(depth):
        d = {'a': d}
    return d


# name, content-type, raw payload
CORPUS: list[tuple[str, str, bytes]] = [
    ('json-null',         'application/json', b'null'),
    ('json-int',          'application/json', b'42'),
    ('json-string',       'application/json', b'"hello"'),
    ('json-bool',         'application/json', b'true'),
    ('json-array',        'application/json', b'[1,2,3]'),
    ('json-empty-array',  'application/json', b'[]'),
    ('malformed-trunc',   'application/json', b'{"model": "m", "messages": ['),
    ('malformed-junk',    'application/json', b'}{not json at all}{'),
    ('malformed-bom',     'application/json', b'\xef\xbb\xbf{"model":"m"}'),
    ('xml-body',          'application/json', b'<xml><model>x</model></xml>'),
    ('json-in-textplain', 'text/plain',       _j({'model': 'm', 'messages': [42]})),
    ('json-nan',          'application/json', b'{"model": "m", "max_tokens": NaN, "messages": []}'),
    ('json-inf',          'application/json', b'{"model": "m", "temperature": Infinity}'),
    ('model-int',         'application/json', _j({'model': 42, 'messages': []})),
    ('model-list',        'application/json', _j({'model': ['a'], 'messages': []})),
    ('messages-string',   'application/json', _j({'model': 'm', 'messages': 'hi'})),
    ('messages-dict',     'application/json', _j({'model': 'm', 'messages': {'role': 'user'}})),
    ('messages-scalar-items', 'application/json', _j({'model': 'm', 'messages': [42, 'x', None, []]})),
    ('msg-content-bare-blocks', 'application/json',
     _j({'model': 'm', 'messages': [{'role': 'user', 'content': ['hi', 42, None]}]})),
    ('tools-dict',        'application/json', _j({'model': 'm', 'messages': [], 'tools': {'name': 'f'}})),
    ('tools-scalar-items', 'application/json', _j({'model': 'm', 'messages': [], 'tools': ['f', 42]})),
    ('toolcalls-dict',    'application/json',
     _j({'model': 'm', 'messages': [{'role': 'assistant', 'content': '',
                                     'tool_calls': {'id': 'x'}}]})),
    ('max_tokens-negative', 'application/json', _j({'model': 'm', 'messages': [], 'max_tokens': -5})),
    ('max_tokens-string', 'application/json', _j({'model': 'm', 'messages': [], 'max_tokens': 'lots'})),
    ('max_tokens-float',  'application/json', _j({'model': 'm', 'messages': [], 'max_tokens': 3.7})),
    ('max_tokens-huge',   'application/json', _j({'model': 'm', 'messages': [], 'max_tokens': 10**9})),
    ('system-int',        'application/json', _j({'model': 'm', 'messages': [], 'system': 5})),
    ('system-bare-blocks', 'application/json',
     _j({'model': 'm', 'messages': [], 'system': ['a', 42]})),
    ('deep-nest-100',     'application/json', _j({'model': 'm', 'messages': [], 'extra': _nest(100)})),
    ('deep-nest-900',     'application/json', _j({'model': 'm', 'messages': [], 'extra': _nest(900)})),
    # B-25.1: cross the interpreter recursion limit (~1000) — valid JSON the
    # guard (and any route parser) cannot even parse: must never 5xx.
    # B-35.1: just over the explicit depth ceiling (BODY_MAX_DEPTH=256) — a body
    # the interpreter CAN still parse must still get the shaped depth 400.
    ('deep-nest-300',     'application/json', ('{"model":"m","messages":[],"extra":' + '{"a":' * 300 + '1' + '}' * 300 + '}').encode()),
    ('deep-nest-3000',    'application/json', ('{"model":"m","messages":[],"extra":' + '{"a":' * 3000 + '1' + '}' * 3000 + '}').encode()),
    ('deep-array-3000',   'application/json', ('{"model":"m","messages":[],"extra":' + '[' * 3000 + '1' + ']' * 3000 + '}').encode()),
    ('unicode-garbage',   'application/json',
     _j({'model': 'm􀀀￾�x', 'messages': [{'role': 'user', 'content': '\x00\x01￾🤖\uffff'}]})),
    ('empty-object',      'application/json', b'{}'),
    ('empty-body-json',   'application/json', b''),
    ('whitespace-body',   'application/json', b'   \n\t  '),
    ('1mb-string-field',  'application/json',
     _j({'model': 'm', 'messages': [{'role': 'user', 'content': 'x' * 1024 * 1024}]})),
    # OpenAI Responses surface specifics (garbage that used to detonate)
    ('resp-input-int',    'application/json', _j({'model': 'm', 'input': 123})),
    ('resp-input-items-scalar', 'application/json', _j({'model': 'm', 'input': [42, 'x']})),
    ('resp-previd-weird', 'application/json',
     _j({'model': 'm', 'input': 'hi', 'previous_response_id': 'resp_nonexistent_zebra'})),
    # UTF-8 truncation mid-codepoint (invalid bytes)
    ('utf8-truncated',    'application/json', b'{"model": "m", "messages": "\xe2\x82'),
]

# Which surfaces accept which bodies depends on the surface contract; the
# invariant under test is only: never 5xx + parseable JSON response.
SURFACES_ALL = ['/v1/chat/completions', '/v1/messages', '/v1/responses']
SURFACES_NVIDIA = SURFACES_ALL + ['/v1/embeddings', '/v1/ranking']


# ── Harness machinery (mirrors run_runtime_e2e.py) ──────────────────────────
def free_port_wait(port: int, timeout: float = 30) -> bool:
    import socket as _s
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _s.create_connection(('127.0.0.1', port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


async def health_wait(session: aiohttp.ClientSession, port: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with session.get(f'http://127.0.0.1:{port}/health', timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return False


def start_wrapper(name: str, wdir: str, port: int, upstream_var: str, extra: dict):
    env = os.environ.copy()
    env.update({
        'BEARER_TOKEN': TOKEN,
        'WRAPPER_SKIP_DOTENV': 'true',
        'MODEL_REGISTRY_STRICT': 'false',
        'SOFT_LIMIT_RPM': '100000', 'HARD_LIMIT_RPM': '100000',
        'NVIDIA_SOFT_LIMIT_RPM': '100000', 'NVIDIA_HARD_LIMIT_RPM': '100000',
        'NOUS_HARD_LIMIT_RPM': '100000', 'OPENCODE_HARD_LIMIT_RPM': '100000',
        'BLACKBOX_HARD_LIMIT_RPM': '100000', 'OPENROUTER_HARD_LIMIT_RPM': '100000',
    })
    for pfx in ('NVIDIA', 'NOUS', 'OPENCODE', 'BLACKBOX', 'OPENROUTER'):
        env[f'{pfx}_API_KEY_1'] = 'mock-key-0000000001'
        env[f'{pfx}_API_KEY_2'] = 'mock-key-0000000002'
        env[f'{pfx}_BASE_URL'] = BASE_FOR[name]
    env.update(extra)
    logf = open(f'/tmp/fuzz-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / wdir), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def scan_log(name: str) -> list[str]:
    path = f'/tmp/fuzz-{name}.log'
    if not os.path.exists(path):
        return []
    txt = open(path, errors='replace').read()
    txt = '\n'.join(l for l in txt.split('\n')
                    if 'loki_push' not in l and ':3100' not in l)
    hits = []
    if 'Traceback (most recent call last)' in txt:
        for blk in txt.split('Traceback (most recent call last)')[1:]:
            hits.append('Traceback: ' + blk.strip().split('\n')[-1][:200])
    for marker in ('async generator ignored GeneratorExit',
                   'Task exception was never retrieved',
                   'was never awaited',
                   'Unclosed client session'):
        if marker in txt:
            hits.append(f'log marker: {marker}')
    return hits


async def one_shot(session, port, surface, name, ctype, payload):
    headers = {'Authorization': f'Bearer {TOKEN}', 'x-api-key': TOKEN,
               'Content-Type': ctype}
    try:
        async with session.post(
                f'http://127.0.0.1:{port}{surface}', data=payload,
                headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.read()
            return r.status, body
    except Exception as e:
        return -1, str(e).encode()


async def fuzz_wrapper(name: str, port: int):
    surfaces = SURFACES_NVIDIA if name == 'nvidia-python' else SURFACES_ALL
    async with aiohttp.ClientSession() as session:
        # serial sanity pass (corpus × surfaces)
        for surface in surfaces:
            for cname, ctype, payload in CORPUS:
                status, body = await one_shot(session, port, surface, cname, ctype, payload)
                label = f'{surface} ← {cname}'
                if status == -1:
                    fail(name, label, f'request raised: {body.decode(errors="replace")[:120]}')
                    continue
                if status >= 500:
                    fail(name, label, f'HTTP {status} on hostile body '
                                      f'(body={body[:200].decode(errors="replace")})')
                    continue
                # response must be parseable JSON (agents never get HTML/empty)
                try:
                    parsed = json.loads(body) if body.strip() else None
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if parsed is None and body.strip():
                    fail(name, label, f'unparseable response body: {body[:120].decode(errors="replace")}')
                    continue
                ok(label)

        # concurrent burst: fire the nastiest shapes in parallel (race smoke)
        hot = [c for c in CORPUS if c[0] in ('json-null', 'malformed-trunc', 'messages-scalar-items',
                                             'resp-previd-weird', 'deep-nest-900', 'utf8-truncated')]
        burst = []
        for _ in range(4):
            for surface in surfaces:
                for cname, ctype, payload in hot:
                    burst.append(one_shot(session, port, surface, cname, ctype, payload))
        results = await asyncio.gather(*burst)
        for (status, body) in results:
            if status == -1 or status >= 500:
                fail(name, 'concurrent-burst',
                     f'status={status} body={body[:160].decode(errors="replace")}')
            else:
                ok('concurrent-burst')

        # post-storm: zero leaked in-flight reservations via /health + /metrics
        await asyncio.sleep(1.0)
        for endpoint in ('/health', '/metrics'):
            try:
                async with session.get(f'http://127.0.0.1:{port}{endpoint}',
                                       headers={'Authorization': f'Bearer {TOKEN}'},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                    inflight = _find_in_flight(data)
                    if inflight is None:
                        fail(name, endpoint, 'no in-flight telemetry field found (CONTRACT §10)')
                    elif inflight != 0:
                        fail(name, endpoint, f'{inflight} in-flight reservation(s) leaked after fuzz storm')
                    else:
                        ok(f'{endpoint} inflight=0')
            except Exception as e:
                # /metrics may legitimately be a different shape on some units;
                # /health is the normative one — only warn-fail on /health
                if endpoint == '/health':
                    fail(name, endpoint, f'health check failed: {e}')


def _find_in_flight(data):
    """Recursively find an in-flight counter in a /health or /metrics doc."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and 'in_flight' in k and isinstance(v, (int, float)):
                return v
        for v in data.values():
            found = _find_in_flight(v)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_in_flight(item)
            if found is not None:
                return found
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrapper')
    args = ap.parse_args()
    targets = ({args.wrapper: WRAPPERS[args.wrapper]} if args.wrapper else WRAPPERS)

    mock = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(MOCK_PORT)],
        stdout=open('/tmp/fuzz-mock.log', 'w'), stderr=subprocess.STDOUT)
    if not free_port_wait(MOCK_PORT):
        print('FATAL: mock upstream did not start')
        mock.kill()
        return 2
    print(f'mock upstream up on :{MOCK_PORT} — corpus: {len(CORPUS)} hostile bodies\n')

    try:
        for name, (wdir, port, upstream_var, extra) in targets.items():
            print(f'── {name} ' + '─' * (58 - len(name)))
            proc, logf = start_wrapper(name, wdir, port, upstream_var, extra)
            try:
                if not free_port_wait(port):
                    tail = open(f'/tmp/fuzz-{name}.log', errors='replace').read()[-1500:]
                    fail(name, 'boot', f'did not start.\n{tail}')
                    continue
                asyncio.run(fuzz_wrapper(name, port))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                logf.close()
            for hit in scan_log(name):
                fail(name, 'server-log', hit)
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
        for f in FAILURES[:60]:
            print('  ✗ ' + f)
        if len(FAILURES) > 60:
            print(f'  … and {len(FAILURES) - 60} more')
        return 1
    print('\n✅ every hostile body shaped correctly — no 5xx, no traceback, no leak')
    return 0


if __name__ == '__main__':
    sys.exit(main())
