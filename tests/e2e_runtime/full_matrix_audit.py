#!/usr/bin/env python3
"""FULL MATRIX AUDIT — every wrapper × upstream × surface × parameter × agent.

Systematic end-to-end audit that boots all 5 wrappers against the OpenAI mock
(COMPATIBILITY_LAYER=1) and the Anthropic mock (COMPATIBILITY_LAYER=2), then
drives every surface with REAL SDK clients (anthropic SDK for Claude Code,
openai SDK for Codex / generic OpenAI SDK) plus raw protocol checks for the
remaining agents (OpenClaw/Hermes/OpenHands via OpenAI chat, OpenCode via all
three, Ollama via discovery).

Every check records PASS/FAIL/BLOCKED with evidence. Results are written to
docs/audits/FULL_MATRIX_AUDIT_2026-08-01.json and the markdown report is
rendered by render_full_matrix_report.py.

Usage:  python tests/e2e_runtime/full_matrix_audit.py
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

OPENAI_MOCK_PORT = 19997
ANTHRO_MOCK_PORT = 19996
TOKEN = 'full-matrix-token'

WRAPPERS = {
    'nvidia-python': (19201, 'NVIDIA', 'root'),
    'nous':          (19202, 'NOUS', 'root'),
    'opencode':      (19203, 'OPENCODE', 'v1'),
    'blackbox':      (19204, 'BLACKBOX', 'v1'),
    'openrouter':    (19206, 'OPENROUTER', 'v1'),
}

RESULTS = []   # list of dicts
CHECK = [0]


def rec(wrapper, layer, component, test, status, evidence='', detail=''):
    CHECK[0] += 1
    RESULTS.append({
        'wrapper': wrapper, 'layer': layer, 'component': component,
        'test': test, 'status': status, 'evidence': evidence[:400], 'detail': detail[:400],
    })
    flag = {'PASS': '✓', 'FAIL': '✗', 'BLOCKED': '⊘'}[status]
    print(f'  {flag} [{wrapper} L{layer}] {component}::{test} — {evidence[:110]}', flush=True)


def log(msg):
    print(msg, flush=True)


# ── process management ────────────────────────────────────────────────────

def start_mock(port, mode):
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    return subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(port), mode],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_wrapper(name, port, pfx, base_style, layer):
    if base_style == 'root':
        base = f'http://127.0.0.1:{ANTHRO_MOCK_PORT}' if layer == 2 else f'http://127.0.0.1:{OPENAI_MOCK_PORT}'
    else:
        base = f'http://127.0.0.1:{ANTHRO_MOCK_PORT}/v1' if layer == 2 else f'http://127.0.0.1:{OPENAI_MOCK_PORT}/v1'
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
        f'{pfx}_API_KEY_2': 'mock-key-0000000002',
        f'{pfx}_BASE_URL': base,
    })
    if name == 'nvidia-python':
        env['NVIDIA_BASE_URL'] = base
        env['MODEL_REGISTRY_STRICT'] = 'false'
        env['VERIFY_ON_BOOT'] = 'false'
    logf = open(f'/tmp/matrix-{name}-l{layer}.log', 'w')
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


# ── http helpers ──────────────────────────────────────────────────────────

async def post(port, path, payload=None, headers=None, raw=None, timeout=60):
    import aiohttp
    hdrs = {'Authorization': f'Bearer {TOKEN}', **({} if headers is None else headers)}
    try:
        async with aiohttp.ClientSession() as s:
            kw = {}
            if raw is not None:
                kw['data'] = raw
            else:
                kw['json'] = payload
            async with s.post(f'http://127.0.0.1:{port}{path}', headers=hdrs, **kw,
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = await r.text()
                return r.status, body, r.headers
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', {}


async def get(port, path, timeout=20):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f'http://127.0.0.1:{port}{path}',
                             headers={'Authorization': f'Bearer {TOKEN}'},
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status, await r.text()
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


# ── real-SDK clients ──────────────────────────────────────────────────────

def sdk_chat(port, payload):
    """generic OpenAI SDK client.chat.completions against a wrapper."""
    import httpx
    from openai import OpenAI
    client = OpenAI(api_key=TOKEN, base_url=f'http://127.0.0.1:{port}/v1',
                    http_client=httpx.Client())
    try:
        if payload.get('stream'):
            body = {k: v for k, v in payload.items() if k != 'stream'}
            with client.chat.completions.create(stream=True, **body) as stream:
                chunks = [c for c in stream]
            return None, chunks
        resp = client.chat.completions.create(**payload)
        return None, resp
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:300]}', None


def sdk_responses(port, payload):
    """Codex / openai SDK client.responses against a wrapper."""
    import httpx
    from openai import OpenAI
    client = OpenAI(api_key=TOKEN, base_url=f'http://127.0.0.1:{port}/v1',
                    http_client=httpx.Client())
    try:
        if payload.get('stream'):
            body = {k: v for k, v in payload.items() if k != 'stream'}
            with client.responses.stream(**body) as stream:
                evs = list(stream)
            return None, evs
        resp = client.responses.create(**payload)
        return None, resp
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:300]}', None


def sdk_anthropic(port, payload):
    """Claude Code / anthropic SDK client.messages against a wrapper."""
    import httpx
    from anthropic import Anthropic
    client = Anthropic(api_key=TOKEN, base_url=f'http://127.0.0.1:{port}',
                       http_client=httpx.Client())
    try:
        if payload.get('stream'):
            body = {k: v for k, v in payload.items() if k != 'stream'}
            with client.messages.stream(**body) as stream:
                msg = stream.get_final_message()
            return None, msg
        msg = client.messages.create(**payload)
        return None, msg
    except Exception as e:
        return f'{type(e).__name__}: {str(e)[:300]}', None


# ── test batteries ────────────────────────────────────────────────────────

async def audit_layer1_chat(port, wrapper):
    c = 'openai-chat'

    # text non-stream via real SDK
    err, resp = sdk_chat(port, {'model': 'mock/normal', 'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'FAIL', err)
    else:
        content = resp.choices[0].message.content
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'PASS' if content else 'FAIL',
            f'content={str(content)[:40]!r}')

    # text stream via real SDK
    err, chunks = sdk_chat(port, {'model': 'mock/normal', 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 1, c, 'sdk-stream-text', 'FAIL', err)
    else:
        text = ''.join(c.choices[0].delta.content or '' for c in chunks if c.choices and c.choices[0].delta)
        rec(wrapper, 1, c, 'sdk-stream-text', 'PASS' if text else 'FAIL', f'text={text[:40]!r}')

    # tools end-to-end (OpenClaw/Hermes/OpenHands pattern)
    err, resp = sdk_chat(port, {'model': 'mock/tools',
                                'messages': [{'role': 'user', 'content': 'call tools'}],
                                'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}},
                                          {'type': 'function', 'function': {'name': 'beta', 'parameters': {'type': 'object'}}}]})
    if err:
        rec(wrapper, 1, c, 'tools-roundtrip', 'FAIL', err)
    else:
        tcs = resp.choices[0].message.tool_calls or []
        ok = len(tcs) == 2 and all(tc.function.arguments for tc in tcs)
        rec(wrapper, 1, c, 'tools-roundtrip', 'PASS' if ok else 'FAIL',
            f'{len(tcs)} tool_calls')

    # parameter passthrough via echo mode
    payload = {'model': 'mock/echo', 'messages': [
                   {'role': 'system', 'content': 'be terse'},
                   {'role': 'user', 'content': 'hi'}],
               'temperature': 0.3, 'top_p': 0.9, 'max_tokens': 123,
               'response_format': {'type': 'json_object'}}
    err, resp = sdk_chat(port, payload)
    if err:
        rec(wrapper, 1, c, 'param-passthrough', 'FAIL', err)
    else:
        echoed = json.loads(resp.choices[0].message.content)
        msgs0 = (echoed.get('messages') or [{}])[0]
        ok = (echoed.get('temperature') == 0.3 and echoed.get('top_p') == 0.9
              and echoed.get('max_tokens') == 123
              and msgs0.get('role') == 'system' and msgs0.get('content') == 'be terse'
              and echoed.get('response_format') == {'type': 'json_object'})
        rec(wrapper, 1, c, 'param-passthrough', 'PASS' if ok else 'FAIL',
            f'temp={echoed.get("temperature")} top_p={echoed.get("top_p")} max={echoed.get("max_tokens")}')

    # multimodal (image) forwarded
    err, resp = sdk_chat(port, {'model': 'mock/echo', 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'what'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]}]})
    if err:
        rec(wrapper, 1, c, 'multimodal', 'FAIL', err)
    else:
        echoed = json.loads(resp.choices[0].message.content)
        msgs = echoed.get('messages', [])
        has_img = any(isinstance(p, dict) and p.get('type') == 'image_url'
                      for m in msgs for p in (m.get('content') if isinstance(m.get('content'), list) else []))
        rec(wrapper, 1, c, 'multimodal', 'PASS' if has_img else 'FAIL',
            'image_url reached upstream' if has_img else 'image dropped')

    # negative: unauthorized
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f'http://127.0.0.1:{port}/v1/chat/completions',
                          json={'model': 'mock/normal', 'messages': [{'role': 'user', 'content': 'hi'}]},
                          headers={'Authorization': 'Bearer WRONG'}) as r:
            rec(wrapper, 1, c, 'auth-401', 'PASS' if r.status == 401 else 'FAIL', f'HTTP {r.status}')

    # negative: malformed JSON
    st, body, _ = await post(port, '/v1/chat/completions', raw=b'{not json',
                             headers={'Content-Type': 'application/json'})
    rec(wrapper, 1, c, 'badjson-4xx', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')

    # negative: non-object body
    st, body, _ = await post(port, '/v1/chat/completions', raw=b'[1,2,3]',
                             headers={'Content-Type': 'application/json'})
    rec(wrapper, 1, c, 'nonobject-4xx', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')

    # negative: invalid max_tokens
    st, body, _ = await post(port, '/v1/chat/completions',
                             {'model': 'mock/normal', 'max_tokens': -5,
                              'messages': [{'role': 'user', 'content': 'hi'}]})
    rec(wrapper, 1, c, 'neg-maxtokens-4xx', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')

    # upstream 500 shaped error (non-stream now handled by the mock)
    st, body, _ = await post(port, '/v1/chat/completions',
                             {'model': 'mock/http500', 'messages': [{'role': 'user', 'content': 'hi'}]})
    shaped = 'error' in body
    # WRAPPER_CONTRACT: all-keys-exhausted returns 429 (never 503); the body
    # must be a shaped error either way.
    rec(wrapper, 1, c, 'upstream-500-shaped', 'PASS' if st >= 400 and shaped else 'FAIL',
        f'HTTP {st} shaped={shaped}')

    # upstream 429 once -> wrapper retries with the next key and succeeds
    st, body, _ = await post(port, '/v1/chat/completions',
                             {'model': 'mock/http429once', 'messages': [{'role': 'user', 'content': 'hi'}]})
    rec(wrapper, 1, c, 'retry-after-429', 'PASS' if st == 200 else 'FAIL', f'HTTP {st}')

    # slow stream with heartbeat (timeout behavior)
    st, body, _ = await post(port, '/v1/chat/completions',
                             {'model': 'mock/slow', 'stream': True,
                              'messages': [{'role': 'user', 'content': 'hi'}]}, timeout=30)
    rec(wrapper, 1, c, 'slow-heartbeat', 'PASS' if st == 200 and ': heartbeat' in body and '[DONE]' in body else 'FAIL',
        f'HTTP {st} hb={": heartbeat" in body} done={"[DONE]" in body}')

    # metadata propagation
    st, body, hdrs = await post(port, '/v1/chat/completions',
                                {'model': 'mock/normal', 'messages': [{'role': 'user', 'content': 'hi'}]},
                                headers={'x-request-id': 'audit-123'})
    got = hdrs.get('x-request-id') or hdrs.get('X-Request-ID') or hdrs.get('x-request-id')
    rec(wrapper, 1, c, 'metadata-x-request-id', 'PASS' if got else 'FAIL', f'rid={got}')


async def audit_layer1_messages(port, wrapper):
    c = 'anthropic-messages'

    # Claude Code non-stream via real anthropic SDK
    err, msg = sdk_anthropic(port, {'model': 'mock/normal', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'FAIL', err)
    else:
        text = ''.join(b.text for b in msg.content if b.type == 'text')
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'PASS' if text else 'FAIL', f'text={text[:40]!r}')

    # Claude Code stream via real anthropic SDK
    err, msg = sdk_anthropic(port, {'model': 'mock/normal', 'max_tokens': 64, 'stream': True,
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 1, c, 'sdk-stream-text', 'FAIL', err)
    else:
        text = ''.join(b.text for b in msg.content if b.type == 'text')
        rec(wrapper, 1, c, 'sdk-stream-text', 'PASS' if text else 'FAIL', f'text={text[:40]!r}')

    # tools via real anthropic SDK (Claude Code tool use)
    err, msg = sdk_anthropic(port, {'model': 'mock/tools', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'call'}],
                                    'tools': [{'name': 'alpha', 'description': 'a', 'input_schema': {'type': 'object'}},
                                              {'name': 'beta', 'description': 'b', 'input_schema': {'type': 'object'}}]})
    if err:
        rec(wrapper, 1, c, 'tools-tool_use', 'FAIL', err)
    else:
        uses = [b for b in msg.content if b.type == 'tool_use']
        ok = len(uses) == 2 and all(u.name for u in uses) and msg.stop_reason == 'tool_use'
        rec(wrapper, 1, c, 'tools-tool_use', 'PASS' if ok else 'FAIL',
            f'{len(uses)} tool_use stop={msg.stop_reason}')

    # thinking block (reasoning mode)
    err, msg = sdk_anthropic(port, {'model': 'mock/reasoning', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'think'}]})
    if err:
        rec(wrapper, 1, c, 'thinking-block', 'FAIL', err)
    else:
        thinking = [b for b in msg.content if b.type == 'thinking']
        rec(wrapper, 1, c, 'thinking-block', 'PASS' if thinking else 'FAIL',
            f'{len(thinking)} thinking blocks')

    # reasoning-only (thinking without text)
    err, msg = sdk_anthropic(port, {'model': 'mock/reasoning_only', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'think'}]})
    if err:
        rec(wrapper, 1, c, 'reasoning-only', 'FAIL', err)
    else:
        rec(wrapper, 1, c, 'reasoning-only', 'PASS', f'stop={msg.stop_reason} blocks={len(msg.content)}')

    # system prompt handled
    err, msg = sdk_anthropic(port, {'model': 'mock/echo', 'max_tokens': 64,
                                    'system': 'sys-42',
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 1, c, 'system-prompt', 'FAIL', err)
    else:
        echoed = json.loads(''.join(b.text for b in msg.content if b.type == 'text'))
        sys_ok = echoed.get('messages', [{}])[0].get('role') == 'system'
        rec(wrapper, 1, c, 'system-prompt', 'PASS' if sys_ok else 'FAIL',
            f'system->messages[0]={"yes" if sys_ok else "no"}')

    # multimodal (base64 image on messages surface)
    err, msg = sdk_anthropic(port, {'model': 'mock/echo', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': [
                                        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}},
                                        {'type': 'text', 'text': 'what'}]}]})
    if err:
        rec(wrapper, 1, c, 'multimodal-image', 'FAIL', err)
    else:
        echoed = json.loads(''.join(b.text for b in msg.content if b.type == 'text'))
        ok = any(isinstance(m.get('content'), list) and any(p.get('type') == 'image_url' for p in m['content'])
                 for m in echoed.get('messages', []))
        rec(wrapper, 1, c, 'multimodal-image', 'PASS' if ok else 'FAIL',
            'image translated to image_url' if ok else 'image dropped')

    # upstream 500 -> shaped anthropic error
    st, body, _ = await post(port, '/v1/messages',
                             {'model': 'mock/http500', 'max_tokens': 64,
                              'messages': [{'role': 'user', 'content': 'hi'}]})
    shaped = '"error"' in body
    rec(wrapper, 1, c, 'upstream-500-shaped', 'PASS' if st >= 400 and shaped else 'FAIL',
        f'HTTP {st} shaped={shaped}')

    # malformed JSON
    st, body, _ = await post(port, '/v1/messages', raw=b'{bad',
                             headers={'Content-Type': 'application/json'})
    rec(wrapper, 1, c, 'badjson-4xx', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')

    # invalid role in messages -> 400
    st, body, _ = await post(port, '/v1/messages',
                             {'model': 'mock/normal', 'max_tokens': 64,
                              'messages': [{'role': 'bogus', 'content': 'x'}]})
    rec(wrapper, 1, c, 'invalid-role-4xx', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')


async def audit_layer1_responses(port, wrapper):
    c = 'openai-responses'

    # Codex non-stream via real SDK
    err, resp = sdk_responses(port, {'model': 'mock/normal', 'input': 'hi'})
    if err:
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'FAIL', err)
    else:
        texts = [p.text for o in resp.output if o.type == 'message'
                 for p in (o.content or []) if p.type == 'output_text']
        rec(wrapper, 1, c, 'sdk-nonstream-text', 'PASS' if texts else 'FAIL', f'text={str(texts)[:40]!r}')

    # Codex stream via real SDK
    err, evs = sdk_responses(port, {'model': 'mock/normal', 'stream': True, 'input': 'hi'})
    if err:
        rec(wrapper, 1, c, 'sdk-stream-text', 'FAIL', err)
    else:
        from openai.types.responses import ResponseTextDeltaEvent
        deltas = [e for e in evs if isinstance(e, ResponseTextDeltaEvent)]
        rec(wrapper, 1, c, 'sdk-stream-text', 'PASS' if deltas else 'FAIL',
            f'{len(deltas)} text deltas')

    # tools via real SDK (Codex function call)
    err, resp = sdk_responses(port, {'model': 'mock/tools', 'input': 'call',
                                     'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}}]})
    if err:
        rec(wrapper, 1, c, 'tools-function_call', 'FAIL', err)
    else:
        fcs = [o for o in resp.output if o.type == 'function_call']
        rec(wrapper, 1, c, 'tools-function_call', 'PASS' if fcs else 'FAIL',
            f'{len(fcs)} function_call')

    # reasoning-only -> completed (CODEX-RESP-01)
    err, resp = sdk_responses(port, {'model': 'mock/reasoning_only', 'input': 'think'})
    if err:
        rec(wrapper, 1, c, 'reasoning-only-completed', 'FAIL', err)
    else:
        rec(wrapper, 1, c, 'reasoning-only-completed', 'PASS' if resp.status == 'completed' else 'FAIL',
            f'status={resp.status}')

    # instructions (developer instructions) passthrough via echo
    err, resp = sdk_responses(port, {'model': 'mock/echo', 'input': 'hi', 'instructions': 'dev-7'})
    if err:
        rec(wrapper, 1, c, 'instructions', 'FAIL', err)
    else:
        texts = [p.text for o in resp.output if o.type == 'message'
                 for p in (o.content or []) if p.type == 'output_text']
        echoed = json.loads(texts[0]) if texts else {}
        msgs = echoed.get('messages', [])
        ok = msgs and msgs[0].get('role') == 'system' and 'dev-7' in msgs[0].get('content', '')
        rec(wrapper, 1, c, 'instructions', 'PASS' if ok else 'FAIL',
            'instructions->system' if ok else 'instructions dropped')

    # max_output_tokens validation
    st, body, _ = await post(port, '/v1/responses',
                             {'model': 'mock/normal', 'input': 'hi', 'max_output_tokens': 99999999999})
    rec(wrapper, 1, c, 'max_output_tokens-cap', 'PASS' if 400 <= st < 500 else 'FAIL', f'HTTP {st}')

    # previous_response_id continuity (multi-turn)
    err, resp = sdk_responses(port, {'model': 'mock/echo', 'input': 'turn one'})
    if err:
        rec(wrapper, 1, c, 'prev-response-id', 'BLOCKED', f'first turn failed: {err}')
    else:
        rid = resp.id
        err2, resp2 = sdk_responses(port, {'model': 'mock/echo', 'input': 'turn two',
                                           'previous_response_id': rid})
        if err2:
            rec(wrapper, 1, c, 'prev-response-id', 'FAIL', err2)
        else:
            rec(wrapper, 1, c, 'prev-response-id', 'PASS', f'{rid[:18]}.. -> turn two ok')

    # upstream error -> response.failed / shaped
    st, body, _ = await post(port, '/v1/responses',
                             {'model': 'mock/midstream_error', 'stream': True, 'input': 'hi'})
    has_failed = 'response.failed' in body or '"error"' in body
    rec(wrapper, 1, c, 'upstream-error-surfaced', 'PASS' if st >= 400 or has_failed else 'FAIL',
        f'HTTP {st} failed={has_failed}')


async def audit_layer1_discovery(port, wrapper):
    c = 'discovery'
    for path in ('/v1/models', '/api/tags', '/health', '/ready', '/v1/capabilities', '/version'):
        st, body = await get(port, path)
        rec(wrapper, 1, c, path, 'PASS' if st == 200 else 'FAIL', f'HTTP {st}')


async def audit_layer2(port, wrapper):
    c = 'anthropic-upstream'

    # chat surface (OpenAI SDK) via translation
    err, resp = sdk_chat(port, {'model': 'mock/normal', 'messages': [{'role': 'user', 'content': 'hi'}]})
    rec(wrapper, 2, c, 'chat-sdk-nonstream', 'PASS' if not err and resp.choices[0].message.content else 'FAIL',
        err or f'content={resp.choices[0].message.content[:30]!r}')

    err, chunks = sdk_chat(port, {'model': 'mock/normal', 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 2, c, 'chat-sdk-stream', 'FAIL', err)
    else:
        text = ''.join(c.choices[0].delta.content or '' for c in chunks if c.choices and c.choices[0].delta)
        rec(wrapper, 2, c, 'chat-sdk-stream', 'PASS' if text else 'FAIL', f'text={text[:30]!r}')

    # chat tools via translation
    err, resp = sdk_chat(port, {'model': 'mock/tools',
                                'messages': [{'role': 'user', 'content': 'call'}],
                                'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}}]})
    if err:
        rec(wrapper, 2, c, 'chat-tools', 'FAIL', err)
    else:
        tcs = resp.choices[0].message.tool_calls or []
        rec(wrapper, 2, c, 'chat-tools', 'PASS' if tcs else 'FAIL', f'{len(tcs)} tool_calls')

    # messages surface = passthrough (anthropic SDK)
    err, msg = sdk_anthropic(port, {'model': 'mock/normal', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
    if err:
        rec(wrapper, 2, c, 'messages-passthrough', 'FAIL', err)
    else:
        text = ''.join(b.text for b in msg.content if b.type == 'text')
        rec(wrapper, 2, c, 'messages-passthrough', 'PASS' if text else 'FAIL', f'text={text[:30]!r}')

    err, msg = sdk_anthropic(port, {'model': 'mock/normal', 'max_tokens': 64, 'stream': True,
                                    'messages': [{'role': 'user', 'content': 'hi'}]})
    rec(wrapper, 2, c, 'messages-passthrough-stream', 'PASS' if not err else 'FAIL', err or 'anthropic stream ok')

    # messages tools passthrough
    err, msg = sdk_anthropic(port, {'model': 'mock/tools', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'call'}],
                                    'tools': [{'name': 'alpha', 'description': 'a', 'input_schema': {'type': 'object'}}]})
    if err:
        rec(wrapper, 2, c, 'messages-tools-passthrough', 'FAIL', err)
    else:
        uses = [b for b in msg.content if b.type == 'tool_use']
        rec(wrapper, 2, c, 'messages-tools-passthrough', 'PASS' if uses else 'FAIL',
            f'{len(uses)} tool_use stop={msg.stop_reason}')

    # messages thinking passthrough
    err, msg = sdk_anthropic(port, {'model': 'mock/reasoning', 'max_tokens': 64,
                                    'messages': [{'role': 'user', 'content': 'think'}]})
    if err:
        rec(wrapper, 2, c, 'messages-thinking-passthrough', 'FAIL', err)
    else:
        th = [b for b in msg.content if b.type == 'thinking']
        rec(wrapper, 2, c, 'messages-thinking-passthrough', 'PASS' if th else 'FAIL', f'{len(th)} thinking')

    # responses surface (Codex) via double translation
    err, resp = sdk_responses(port, {'model': 'mock/normal', 'input': 'hi'})
    rec(wrapper, 2, c, 'responses-sdk-nonstream', 'PASS' if not err else 'FAIL', err or 'sdk parsed ok')

    err, evs = sdk_responses(port, {'model': 'mock/normal', 'stream': True, 'input': 'hi'})
    rec(wrapper, 2, c, 'responses-sdk-stream', 'PASS' if not err else 'FAIL', err or f'{len(evs)} events')

    err, resp = sdk_responses(port, {'model': 'mock/tools', 'input': 'call',
                                     'tools': [{'type': 'function', 'function': {'name': 'alpha', 'parameters': {'type': 'object'}}}]})
    if err:
        rec(wrapper, 2, c, 'responses-tools', 'FAIL', err)
    else:
        fcs = [o for o in resp.output if o.type == 'function_call']
        rec(wrapper, 2, c, 'responses-tools', 'PASS' if fcs else 'FAIL', f'{len(fcs)} function_call')

    # error handling layer 2 — anthropic mock returns a shaped error for
    # model=mock/error (see mock_upstream anthropic handler)
    st, body, _ = await post(port, '/v1/chat/completions',
                             {'model': 'mock/error', 'messages': [{'role': 'user', 'content': 'hi'}]})
    rec(wrapper, 2, c, 'chat-upstream-error', 'PASS' if st >= 400 and 'error' in body else 'FAIL',
        f'HTTP {st} shaped={"error" in body}')


async def main():
    openai_mock = start_mock(OPENAI_MOCK_PORT, 'openai')
    anthro_mock = start_mock(ANTHRO_MOCK_PORT, 'anthropic')
    time.sleep(1.5)
    procs = {}
    try:
        # ── layer 1 (OpenAI upstream) ──
        log('═══ COMPATIBILITY_LAYER=1 (OpenAI upstream) ═══')
        for name, (port, pfx, style) in WRAPPERS.items():
            log(f'── {name} L1 ──')
            p, lf = start_wrapper(name, port, pfx, style, 1)
            procs[(name, 1)] = (p, lf)
            if not wait_health(port):
                rec(name, 1, 'boot', 'health', 'FAIL', 'server not healthy')
                continue
            await audit_layer1_discovery(port, name)
            await audit_layer1_chat(port, name)
            await audit_layer1_messages(port, name)
            await audit_layer1_responses(port, name)
            p.terminate()

        # ── layer 2 (Anthropic upstream) ──
        log('═══ COMPATIBILITY_LAYER=2 (Anthropic upstream) ═══')
        for name, (port, pfx, style) in WRAPPERS.items():
            log(f'── {name} L2 ──')
            p, lf = start_wrapper(name, port, pfx, style, 2)
            procs[(name, 2)] = (p, lf)
            if not wait_health(port):
                rec(name, 2, 'boot', 'health', 'FAIL', 'server not healthy')
                continue
            await audit_layer2(port, name)
            p.terminate()

        # wait for all to die
        for (p, lf) in procs.values():
            try:
                p.terminate()
            except Exception:
                pass

        # ── summary ──
        passed = sum(1 for r in RESULTS if r['status'] == 'PASS')
        failed = sum(1 for r in RESULTS if r['status'] == 'FAIL')
        blocked = sum(1 for r in RESULTS if r['status'] == 'BLOCKED')
        log('')
        log(f'═ TOTAL: {len(RESULTS)} checks | PASS={passed} FAIL={failed} BLOCKED={blocked} ═')
        if failed:
            log('FAILURES:')
            for r in RESULTS:
                if r['status'] == 'FAIL':
                    log(f"  ✗ [{r['wrapper']} L{r['layer']}] {r['component']}::{r['test']} — {r['evidence']}")
        out = ROOT / 'docs/audits/FULL_MATRIX_AUDIT_2026-08-01.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'total': len(RESULTS), 'passed': passed,
                                   'failed': failed, 'blocked': blocked,
                                   'results': RESULTS}, indent=1))
        log(f'evidence -> {out}')
        sys.exit(1 if failed else 0)
    finally:
        for (p, lf) in procs.values():
            try:
                p.terminate()
            except Exception:
                pass
        openai_mock.terminate()
        anthro_mock.terminate()


if __name__ == '__main__':
    asyncio.run(main())
