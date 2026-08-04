#!/usr/bin/env python3
"""COMPATIBILITY_LAYER E2E — boots every wrapper against an Anthropic-native
upstream (layer=2) and verifies auto-discovery (layer=3).

Layer=2 checks per wrapper × surface:
  /v1/chat/completions  -> translated OpenAI Chat request, Anthropic response
                           translated back to OpenAI shape (stream + non-stream)
  /v1/messages          -> passthrough (Anthropic shape both ways)
  /v1/responses         -> Responses -> Chat -> Anthropic -> back (stream +
                           non-stream; streaming also parsed by the openai SDK)

Layer=3 checks auto-discovery: openrouter against the OpenAI mock (must pick
layer 1) and against the Anthropic mock (must pick layer 2).

Requires: pip install -r tests/requirements.txt (incl. openai).
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OPENAI_MOCK_PORT = 19999
ANTHRO_MOCK_PORT = 19998
TOKEN = 'compat-e2e-token'

# base style: 'root' -> base has no /v1 (wrapper appends /v1), 'v1' -> base
# includes /v1 (wrapper appends /messages)
WRAPPERS = {
    'nvidia-python': (19101, 'NVIDIA', 'root'),
    'nous':          (19102, 'NOUS', 'root'),
    'opencode':      (19103, 'OPENCODE', 'v1'),
    'blackbox':      (19104, 'BLACKBOX', 'v1'),
    'openrouter':    (19106, 'OPENROUTER', 'v1'),
}
FAILURES = []


def log(msg):
    print(msg, flush=True)


def start_mock(port, mode):
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    return subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(port), mode],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_wrapper(name, port, pfx, base_style, layer, base_port=None):
    mock_port = base_port if base_port is not None else ANTHRO_MOCK_PORT
    base = f'http://127.0.0.1:{mock_port}' if base_style == 'root' \
        else f'http://127.0.0.1:{mock_port}/v1'
    env = os.environ.copy()
    env.update({
        'WRAPPER_SKIP_DOTENV': 'true',
        'BEARER_TOKEN': TOKEN,
        'LISTEN_PORT': str(port),
        'LISTEN_HOST': '127.0.0.1',
        'RATE_LIMIT_RPM': '0',
        'HEARTBEAT_INTERVAL_MS': '300',
        'COMPATIBILITY_LAYER': str(layer),
        'PYTHONPATH': f'{ROOT}:{ROOT / name}',
        'PYTHONUNBUFFERED': '1',
        'FREE_ONLY': 'no',
        'SOFT_LIMIT_RPM': '100000',
        'HARD_LIMIT_RPM': '100000',
        f'{pfx}_API_KEY_1': 'mock-key-0000000001',
        f'{pfx}_BASE_URL': base,
    })
    if name == 'nvidia-python':
        env['NVIDIA_BASE_URL'] = base
        env['NVIDIA_API_KEY_1'] = 'mock-key-0000000001'
        env['MODEL_REGISTRY_STRICT'] = 'false'
        env['VERIFY_ON_BOOT'] = 'false'
    logf = open(f'/tmp/compat-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / name), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def wait_health(port, timeout=60):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_port_free(port, timeout=30):
    """R17 fix (gate flake): after terminate(), the old process keeps serving
    during graceful drain — a successor that binds the same port may never
    come up while /health probes hit the DYING listener (observed flake:
    layer=3 Anthropic-mock probe answered by the previous OpenAI-configured
    process → upstream 404). Terminate → reap → wait for real connection
    refusal before booting the next instance."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', port))
        except (ConnectionRefusedError, OSError):
            s.close()
            return True
        else:
            s.close()
            time.sleep(0.3)
    return False


def stop_proc(p, port):
    """Terminate + reap + port-free wait (see wait_port_free docstring)."""
    try:
        p.terminate()
        p.wait(timeout=15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    wait_port_free(port)


async def post(port, path, payload, headers, stream):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f'http://127.0.0.1:{port}{path}', json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                raw = await r.text()
                return r.status, raw
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'


def parse_sse(raw):
    events = []
    for block in raw.replace('\r\n', '\n').split('\n\n'):
        block = block.strip('\n')
        if not block.strip():
            continue
        ev = None
        datas = []
        for line in block.split('\n'):
            if line.startswith(':'):
                continue
            if line.startswith('event:'):
                ev = line[6:].strip()
            elif line.startswith('data:'):
                v = line[5:].strip()
                if v:
                    datas.append(v)
        if not datas:
            continue
        payload = '\n'.join(datas)
        if payload == '[DONE]':
            events.append((ev, '[DONE]'))
            continue
        try:
            events.append((ev, json.loads(payload)))
        except json.JSONDecodeError:
            events.append((ev, payload))
    return events


async def sdk_parse_responses(body, label):
    import httpx
    from openai import OpenAI

    def make_transport(b):
        def handler(request):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=b.encode())
        return httpx.MockTransport(handler)

    client = OpenAI(api_key='sk-test', http_client=httpx.Client(transport=make_transport(body)))
    try:
        with client.responses.stream(model='m', input='hi') as stream:
            return None, list(stream)
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:250]}', None


async def exercise_layer2(name, port):
    headers = {'Authorization': f'Bearer {TOKEN}'}

    # ── /v1/chat/completions (stream + non-stream) ──
    for stream in (False, True):
        st, raw = await post(port, '/v1/chat/completions',
                             {'model': 'mock/normal', 'stream': stream,
                              'messages': [{'role': 'user', 'content': 'hi'}]}, headers, stream)
        if st != 200:
            FAILURES.append(f'{name} chat stream={stream}: HTTP {st}: {raw[:150]}')
            continue
        if stream:
            evs = parse_sse(raw)
            if 'data: [DONE]' not in raw or 'anthropic mock' not in raw:
                FAILURES.append(f'{name} chat stream: missing [DONE]/text: {raw[:150]}')
            else:
                log(f'  {name} chat stream: OK')
        else:
            d = json.loads(raw)
            if not d.get('choices') or not d['choices'][0]['message'].get('content'):
                FAILURES.append(f'{name} chat non-stream: bad shape: {raw[:150]}')
            else:
                log(f'  {name} chat non-stream: OK')

    # ── /v1/messages (passthrough, stream + non-stream) ──
    for stream in (False, True):
        st, raw = await post(port, '/v1/messages',
                             {'model': 'mock/normal', 'max_tokens': 64, 'stream': stream,
                              'messages': [{'role': 'user', 'content': 'hi'}]}, headers, stream)
        if st != 200:
            FAILURES.append(f'{name} messages stream={stream}: HTTP {st}: {raw[:150]}')
            continue
        if stream:
            evs = parse_sse(raw)
            types = [d.get('type') for _e, d in evs if isinstance(d, dict)]
            if 'message_start' not in types or 'message_stop' not in types:
                FAILURES.append(f'{name} messages stream: bad lifecycle: {types}')
            elif 'data: [DONE]' in raw:
                FAILURES.append(f'{name} messages stream: [DONE] leaked into Anthropic passthrough')
            else:
                log(f'  {name} messages stream: OK')
        else:
            d = json.loads(raw)
            if d.get('type') != 'message':
                FAILURES.append(f'{name} messages non-stream: not a message: {raw[:150]}')
            else:
                log(f'  {name} messages non-stream: OK')

    # ── /v1/responses (stream parsed by the openai SDK + non-stream) ──
    st, raw = await post(port, '/v1/responses',
                         {'model': 'mock/normal', 'stream': True, 'input': 'hi'}, headers, True)
    if st != 200:
        FAILURES.append(f'{name} responses stream: HTTP {st}: {raw[:150]}')
    else:
        err, evs = await sdk_parse_responses(raw, f'{name} responses')
        if err:
            FAILURES.append(f'{name} responses stream: SDK rejected: {err}')
        else:
            log(f'  {name} responses stream: SDK OK ({len(evs)} events)')
    st, raw = await post(port, '/v1/responses',
                         {'model': 'mock/normal', 'stream': False, 'input': 'hi'}, headers, False)
    if st != 200:
        FAILURES.append(f'{name} responses non-stream: HTTP {st}: {raw[:150]}')
    else:
        d = json.loads(raw)
        if d.get('object') != 'response' or not d.get('output'):
            FAILURES.append(f'{name} responses non-stream: bad shape: {raw[:150]}')
        else:
            log(f'  {name} responses non-stream: OK')

    # ── tool round trip via chat surface (layer-2 tools) ──
    st, raw = await post(port, '/v1/chat/completions',
                         {'model': 'mock/tools', 'stream': False,
                          'messages': [{'role': 'user', 'content': 'call tools'}],
                          'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}}]},
                         headers, False)
    if st == 200:
        d = json.loads(raw)
        tc = d.get('choices', [{}])[0].get('message', {}).get('tool_calls')
        if not tc:
            FAILURES.append(f'{name} chat tools: no tool_calls translated back: {raw[:200]}')
        else:
            log(f'  {name} chat tools: OK ({[t["function"]["name"] for t in tc]})')
    else:
        FAILURES.append(f'{name} chat tools: HTTP {st}: {raw[:150]}')


async def main():
    log('starting mocks...')
    openai_mock = start_mock(OPENAI_MOCK_PORT, 'openai')
    anthro_mock = start_mock(ANTHRO_MOCK_PORT, 'anthropic')
    time.sleep(1.5)
    procs = {}
    try:
        log('── COMPATIBILITY_LAYER=2 (Anthropic upstream) ──')
        for name, (port, pfx, style) in WRAPPERS.items():
            log(f'starting {name} (layer=2)...')
            p, lf = start_wrapper(name, port, pfx, style, 2)
            procs[name] = (p, lf)
        for name, (port, _pfx, _style) in WRAPPERS.items():
            if not wait_health(port):
                FAILURES.append(f'{name}: did not become healthy')
                log(f'  {name}: NOT HEALTHY')
                continue
            await exercise_layer2(name, port)

        # ── COMPATIBILITY_LAYER=3 auto-discovery — ALL 5 wrappers × both mocks ──
        # R17 (B-17.1): previously this section exercised openrouter only, and
        # its Anthropic-mock case could "pass" against a still-draining older
        # process (the dialect was never actually probed — no wrapper consumed
        # the probe until R17). Every wrapper now proves real auto-discovery:
        # OpenAI mock → chat completions shape; Anthropic mock → message shape.
        log('── COMPATIBILITY_LAYER=3 (auto-discovery) ──')
        for name, (port, pfx, style) in WRAPPERS.items():
            if name in procs:
                stop_proc(procs[name][0], port)
        for name, (port, pfx, style) in WRAPPERS.items():
            for mock_port, dialect, path, payload, expect_msg in (
                (OPENAI_MOCK_PORT, 'OpenAI', '/v1/chat/completions',
                 {'model': 'mock/normal', 'stream': False,
                  'messages': [{'role': 'user', 'content': 'hi'}]}, False),
                (ANTHRO_MOCK_PORT, 'Anthropic', '/v1/messages',
                 {'model': 'mock/normal', 'max_tokens': 64, 'stream': False,
                  'messages': [{'role': 'user', 'content': 'hi'}]}, True),
            ):
                p, lf = start_wrapper(name, port, pfx, style, 3, base_port=mock_port)
                procs[f'{name}-l3-{dialect}'] = (p, lf)
                if wait_health(port):
                    st, raw = await post(port, path, payload,
                                         {'Authorization': f'Bearer {TOKEN}'}, False)
                    d = json.loads(raw) if st == 200 else {}
                    ok = (d.get('type') == 'message') if expect_msg else bool(d.get('choices'))
                    if st == 200 and ok:
                        log(f'  {name} layer=3 vs {dialect} mock: OK (detected {dialect})')
                    else:
                        FAILURES.append(f'{name} layer=3 vs {dialect} mock: HTTP {st}: {raw[:150]}')
                else:
                    FAILURES.append(f'{name} layer=3 vs {dialect} mock: not healthy')
                stop_proc(p, port)

        log('')
        if FAILURES:
            log(f'FAILURES ({len(FAILURES)}):')
            for f in FAILURES:
                log('  ✗ ' + f)
            sys.exit(1)
        log('✅ COMPATIBILITY_LAYER=2 AND =3 VERIFIED ACROSS ALL 5 WRAPPERS')
    finally:
        for p, lf in procs.values():
            try:
                p.terminate()
            except Exception:
                pass
        openai_mock.terminate()
        anthro_mock.terminate()


if __name__ == '__main__':
    asyncio.run(main())
