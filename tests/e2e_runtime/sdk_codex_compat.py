#!/usr/bin/env python3
"""CODEX-RESP-02 proof: every wrapper's /v1/responses streaming output must be
parsable by the official openai SDK stream parser (the exact code path Codex
uses) for text, tool-call, and reasoning-only streams.

Before the fix, all five wrappers emitted:
  - response.created with a minimal {id, model, status} -> the SDK snapshot's
    `output` was None -> AttributeError on the first output_item.added
  - response.function_call.delta (wrong name) -> tool arguments never
    accumulated by the SDK
  - reasoning items with summary:'' (string) -> SDK serializer warnings
After the fix, the SDK must parse every stream and accumulate tool arguments.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MOCK_PORT = 19999
TOKEN = 'sdk-e2e-token'
BASE_FOR = {
    'nvidia-python': f'http://127.0.0.1:{MOCK_PORT}',
    'nous':       f'http://127.0.0.1:{MOCK_PORT}',
    'opencode':   f'http://127.0.0.1:{MOCK_PORT}/v1',
    'blackbox':   f'http://127.0.0.1:{MOCK_PORT}/v1',
    'openrouter': f'http://127.0.0.1:{MOCK_PORT}/v1',
}
PORT = {'nvidia-python': 19101, 'nous': 9102, 'opencode': 9103, 'blackbox': 9104, 'openrouter': 9106}
FAILURES = []


def log(msg):
    print(msg, flush=True)


def start_mock():
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    p = subprocess.Popen([sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(MOCK_PORT)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def start_wrapper(name: str, wdir: str, port: int, upstream_var: str):
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
        'SOFT_LIMIT_RPM': '100000',
        'HARD_LIMIT_RPM': '100000',
        f'{name.upper()}_HARD_LIMIT_RPM'.replace('NOUS', 'NOUS').replace('OPENCODE', 'OPENCODE'): '100000',
    })
    for pfx in ('NVIDIA', 'NOUS', 'OPENCODE', 'BLACKBOX', 'OPENROUTER'):
        env[f'{pfx}_API_KEY_1'] = 'mock-key-0000000001'
        env[f'{pfx}_BASE_URL'] = BASE_FOR[name]
    logf = open(f'/tmp/sdk-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / wdir), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def wait_health(port, timeout=60):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/health')
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


async def sdk_parse(body: str, label: str):
    import httpx
    from openai import OpenAI

    def make_transport(b):
        def handler(request):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=b.encode())
        return httpx.MockTransport(handler)

    client = OpenAI(api_key='sk-test', http_client=httpx.Client(transport=make_transport(body)))
    try:
        with client.responses.stream(model='m', input='hi') as stream:
            evs = list(stream)
        # verify tool arguments accumulated end-to-end
        return None, evs
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:300]}', None


async def drive(port, payload, headers):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f'http://127.0.0.1:{port}/v1/responses', json=payload,
                              headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
                raw = await r.text()
                return r.status, raw
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'


async def sdk_parse_nonstream(body: str, label: str):
    """Parse a non-streaming Responses JSON body with the openai SDK's strict
    Response model — the exact path `client.responses.create()` uses."""
    import httpx
    from openai import OpenAI

    def make_transport(b):
        def handler(request):
            return httpx.Response(200, headers={'Content-Type': 'application/json'}, content=b.encode())
        return httpx.MockTransport(handler)

    client = OpenAI(api_key='sk-test', http_client=httpx.Client(transport=make_transport(body)))
    try:
        resp = client.responses.create(model='m', input='hi')
        return None, resp
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:300]}', None


async def main():
    log('starting mock upstream...')
    mock = start_mock()
    time.sleep(1.5)
    wrappers = {}
    try:
        for name in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
            log(f'starting {name}...')
            p, lf = start_wrapper(name, name, PORT[name], f'{name.upper()}_BASE_URL'.replace('NVIDIA-PYTHON', 'NVIDIA'))
            wrappers[name] = (p, lf)
        for name in wrappers:
            if not wait_health(PORT[name]):
                FAILURES.append(f'{name}: server did not become healthy')
                log(f'  {name}: NOT HEALTHY')

        headers = {'Authorization': f'Bearer {TOKEN}'}
        for name in wrappers:
            if any(f.startswith(f'{name}:') for f in FAILURES):
                continue
            for mode, payload in (
                ('tools', {'model': 'mock/tools', 'stream': True, 'input': 'hi',
                           'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}},
                                     {'type': 'function', 'function': {'name': 'beta', 'parameters': {'type': 'object'}}}]}),
                ('reasoning_only', {'model': 'mock/reasoning_only', 'stream': True, 'input': 'hi'}),
                ('reasoning', {'model': 'mock/reasoning', 'stream': True, 'input': 'hi'}),
            ):
                st, raw = await drive(PORT[name], payload, headers)
                if st != 200:
                    FAILURES.append(f'{name} [{mode}]: HTTP {st}: {raw[:200]}')
                    log(f'  {name} [{mode}]: HTTP {st}')
                    continue
                err, evs = await sdk_parse(raw, f'{name} [{mode}]')
                if err:
                    FAILURES.append(f'{name} [{mode}]: SDK rejected stream: {err}')
                    log(f'  {name} [{mode}]: SDK REJECTED: {err}')
                    continue
                log(f'  {name} [{mode}]: SDK OK ({len(evs)} typed events)')
                if mode == 'tools':
                    # find the accumulated arguments in the SDK-parsed events.
                    # The SDK fires the typed event classes directly.
                    names = [type(e).__name__ for e in evs]
                    deltas = [e for e in evs if 'FunctionCallArgumentsDelta' in type(e).__name__]
                    dones = [e for e in evs if 'FunctionCallArgumentsDone' in type(e).__name__]
                    if not deltas:
                        FAILURES.append(f'{name} [tools]: no function_call_arguments.delta events: {names}')
                        log(f'    classes: {names}')
                    if not dones:
                        FAILURES.append(f'{name} [tools]: no function_call_arguments.done events: {names}')
                        log(f'    classes: {names}')
                if deltas and dones:
                    log(f'    tool args streamed via {len(deltas)} deltas + {len(dones)} done events')
            # non-streaming Responses must also parse with the SDK's strict
            # Response model (client.responses.create path)
            st, raw = await drive(PORT[name],
                                  {'model': 'mock/tools', 'stream': False, 'input': 'hi',
                                   'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}}]},
                                  headers)
            if st != 200:
                FAILURES.append(f'{name} [nonstream]: HTTP {st}: {raw[:200]}')
                log(f'  {name} [nonstream]: HTTP {st}')
                continue
            err, _resp = await sdk_parse_nonstream(raw, f'{name} [nonstream]')
            if err:
                FAILURES.append(f'{name} [nonstream]: SDK rejected response: {err}')
                log(f'  {name} [nonstream]: SDK REJECTED: {err}')
            else:
                otypes = [o.type for o in _resp.output]
                log(f'  {name} [nonstream]: SDK OK ({otypes})')
        log('')
        if FAILURES:
            log(f'FAILURES ({len(FAILURES)}):')
            for f in FAILURES:
                log('  ✗ ' + f)
            sys.exit(1)
        log('✅ ALL WRAPPERS × MODES PARSE CLEANLY WITH THE OPENAI SDK (CODEX-RESP-02 FIXED)')
    finally:
        for p, lf in wrappers.values():
            p.terminate()
        mock.terminate()


if __name__ == '__main__':
    asyncio.run(main())
