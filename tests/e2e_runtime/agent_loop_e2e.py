#!/usr/bin/env python3
"""Agent-loop E2E — drives every wrapper as a REAL uvicorn server with the REAL
anthropic and openai SDKs, exactly the way Claude Code / Codex / openclaw /
hermes / opencode / openhands consume the backends.

Round-6 audit (2026-08-04): the runtime harness proved SSE-level correctness;
agents add two more failure classes that only strict SDK clients surface:

  * strict typed parsing (pydantic) of every event/block — an index reused
    after close, a missing content_block_stop, truncated input_json_delta, or
    a shape drift raises INSIDE the SDK ("respon parsing tidak sesuai"),
  * tool-use round trips: tool_use → tool_result → next turn (Claude Code),
    tool_calls → role:tool (OpenAI SDK), function_call_output +
    previous_response_id replay (Codex). A broken mapping makes the agent
    loop die on turn 2 ("proses berhenti di tengah jalan").

The anthropic client is configured the way Claude Code configures it: BOTH
Authorization: Bearer (auth_token) AND x-api-key (api_key) on every request,
so the P0-2 dual-header auth contract is exercised on every call.

Coverage per wrapper (all through the real SDKs):
  1. /v1/messages stream  — mode=tools:       strict stream → tool_use(args) →
                                            tool_result echo turn (upstream
                                            receives role:tool w/ matching id)
  2. /v1/messages stream  — mode=dsml_stream: MiniMax DSML markup recovered as
                                            a REAL tool_use the SDK can execute
  3. /v1/messages nonstream — mode=tools:    strict Message parse, stop_reason
  4. /v1/messages stream  — mode=slow:       heartbeats keep the SDK alive
  5. /v1/chat/completions nonstream — tools loop + role:tool echo turn
  6. /v1/chat/completions stream    — tools chunks parse via OpenAI SDK
  7. /v1/responses nonstream — tools turn, then echo turn with
                               previous_response_id + function_call_output:
                               the replayed history must contain the assistant
                               tool_calls AND matching role:tool entries
                               (orphan-tool 400 class), all 5 wrappers
  8. error surfaces — mode=http500: SDK raises with a SHAPED error body
                      (AnthropicError / OpenAIError carries the message,
                      never an SSE/JSON parse error)

Usage:  python tests/e2e_runtime/agent_loop_e2e.py [--wrapper NAME] [-v]
Exit code 0 = every agent loop works end to end on every wrapper.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

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

FAILURES: list[str] = []
CHECKS = [0]
VERBOSE = False


def fail(wrapper: str, what: str, msg: str):
    entry = f'[{wrapper}] {what}: {msg}'
    FAILURES.append(entry)
    print(f'  ✗ {entry}', flush=True)


def ok(wrapper: str, what: str, note: str = ''):
    CHECKS[0] += 1
    if VERBOSE:
        print(f'  ✓ [{wrapper}] {what} {note}', flush=True)


def free_port_wait(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def health_wait(port: int, timeout: float = 90.0) -> bool:
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
        'SOFT_LIMIT_RPM': '100000',
        'HARD_LIMIT_RPM': '100000',
        'NVIDIA_SOFT_LIMIT_RPM': '100000',
        'NVIDIA_HARD_LIMIT_RPM': '100000',
        'NOUS_HARD_LIMIT_RPM': '100000',
        'OPENCODE_HARD_LIMIT_RPM': '100000',
        'BLACKBOX_HARD_LIMIT_RPM': '100000',
        'OPENROUTER_HARD_LIMIT_RPM': '100000',
    })
    for pfx in ('NVIDIA', 'NOUS', 'OPENCODE', 'BLACKBOX', 'OPENROUTER'):
        env[f'{pfx}_API_KEY_1'] = 'mock-key-0000000001'
        env[f'{pfx}_API_KEY_2'] = 'mock-key-0000000002'
        env[f'{pfx}_BASE_URL'] = BASE_FOR[name]
    env.update(extra)

    logf = open(f'/tmp/al-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / wdir), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def scan_log(name: str) -> list[str]:
    path = f'/tmp/al-{name}.log'
    if not os.path.exists(path):
        return []
    txt = open(path, errors='replace').read()
    txt = '\n'.join(l for l in txt.split('\n')
                    if 'loki_push' not in l and ':3100' not in l)
    hits = []
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


# ── SDK clients ────────────────────────────────────────────────────────────

def _anthropic_client(port: int):
    import anthropic
    return anthropic.Anthropic(
        base_url=f'http://127.0.0.1:{port}',
        api_key=TOKEN,        # x-api-key
        auth_token=TOKEN,     # Authorization: Bearer — Claude Code sends both
        timeout=30.0,
        max_retries=0,
    )


def _openai_client(port: int):
    import openai
    return openai.OpenAI(
        base_url=f'http://127.0.0.1:{port}/v1',
        api_key=TOKEN,
        timeout=30.0,
        max_retries=0,
    )


_TOOLS_ANTHROPIC = [{
    'name': 'alpha',
    'description': 'test tool alpha',
    'input_schema': {'type': 'object', 'properties': {'x': {'type': 'number'}}, 'required': ['x']},
}, {
    'name': 'beta',
    'description': 'test tool beta',
    'input_schema': {'type': 'object', 'properties': {'y': {'type': 'number'}}, 'required': ['y']},
}]

_TOOLS_OPENAI = [
    {'type': 'function', 'function': {'name': 'alpha', 'description': 'test tool alpha',
                                      'parameters': {'type': 'object', 'properties': {'x': {'type': 'number'}}, 'required': ['x']}}},
    {'type': 'function', 'function': {'name': 'beta', 'description': 'test tool beta',
                                      'parameters': {'type': 'object', 'properties': {'y': {'type': 'number'}}, 'required': ['y']}}},
]

_WEATHER_TOOL_ANTHROPIC = [{
    'name': 'get_weather',
    'description': 'get weather for a city',
    'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city']},
}]


def _blocks(msg) -> list:
    return list(getattr(msg, 'content', []) or [])


def check_anthropic_tool_loop_stream(wrapper: str, port: int):
    """Claude Code shape: stream turn 1 (structured tool calls), execute the
    tools, then turn 2 with tool_result — echoed upstream body must carry a
    role:tool entry per tool_use id (broken mapping = upstream 400 = agent
    dies mid-loop)."""
    ac = _anthropic_client(port)
    try:
        with ac.messages.stream(
                model='mock/tools', max_tokens=256, tools=_TOOLS_ANTHROPIC,
                messages=[{'role': 'user', 'content': 'call both tools'}]) as s:
            msg = s.get_final_message()
    except Exception as e:
        fail(wrapper, 'anthropic.stream.tools', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    tool_uses = [b for b in _blocks(msg) if getattr(b, 'type', '') == 'tool_use']
    if msg.stop_reason != 'tool_use':
        fail(wrapper, 'anthropic.stream.tools', f'stop_reason={msg.stop_reason!r}, want tool_use')
        return
    if len(tool_uses) != 2:
        fail(wrapper, 'anthropic.stream.tools', f'{len(tool_uses)} tool_use blocks, want 2')
        return
    for b, want in zip(sorted(tool_uses, key=lambda b: b.name), ('alpha', 'beta')):
        if b.name != want:
            fail(wrapper, 'anthropic.stream.tools', f'tool name {b.name!r} want {want!r}')
            return
        # SDK merged input_json_delta into a REAL object — corrupt partial_json
        # would already have raised during accumulation.
        if not isinstance(b.input, dict):
            fail(wrapper, 'anthropic.stream.tools', f'tool input not an object: {b.input!r}')
            return
    # ── turn 2: tool_result for every tool_use, echoed back upstream ──
    tr_content = []
    for b in sorted(tool_uses, key=lambda b: b.name):
        tr_content.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': 'ok'})
    convo = [
        {'role': 'user', 'content': 'call both tools'},
        {'role': 'assistant', 'content': [{'type': b.type, **b.model_dump(exclude={'type'})} for b in _blocks(msg)]},
        {'role': 'user', 'content': tr_content},
    ]
    try:
        m2 = ac.messages.create(model='mock/echo', max_tokens=4096, messages=convo)
    except Exception as e:
        fail(wrapper, 'anthropic.tool_result_turn', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    echo_text = ''.join(getattr(b, 'text', '') for b in _blocks(m2) if getattr(b, 'type', '') == 'text')
    try:
        fwd = json.loads(echo_text[echo_text.index('{'):])
        fwd_msgs = [m for m in fwd.get('messages', []) if isinstance(m, dict)]
    except Exception as e:
        fail(wrapper, 'anthropic.tool_result_turn', f'echo body unreadable: {str(e)[:160]}')
        return
    tool_msgs = {m.get('tool_call_id'): m for m in fwd_msgs if m.get('role') == 'tool'}
    missing = [b.id for b in tool_uses if b.id not in tool_msgs]
    if missing:
        fail(wrapper, 'anthropic.tool_result_turn',
             f'upstream got NO role:tool message for tool_use ids {missing} (orphan → upstream 400)')
        return
    if m2.stop_reason != 'end_turn':
        fail(wrapper, 'anthropic.tool_result_turn', f'turn2 stop_reason={m2.stop_reason!r}')
        return
    ok(wrapper, 'anthropic.tool_loop', 'stream tools → tool_result → end_turn')


def check_anthropic_dsml_loop(wrapper: str, port: int):
    """MiniMax shape: DSML markup in the text channel must surface through the
    SDK as a REAL, executable tool_use (recovered by the wrapper), with
    stop_reason=tool_use — otherwise the agent turn just ends."""
    ac = _anthropic_client(port)
    try:
        with ac.messages.stream(
                model='mock/dsml_stream', max_tokens=256, tools=_WEATHER_TOOL_ANTHROPIC,
                messages=[{'role': 'user', 'content': 'weather in Jakarta?'}]) as s:
            msg = s.get_final_message()
    except Exception as e:
        fail(wrapper, 'anthropic.stream.dsml', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    tool_uses = [b for b in _blocks(msg) if getattr(b, 'type', '') == 'tool_use']
    text = ''.join(getattr(b, 'text', '') for b in _blocks(msg) if getattr(b, 'type', '') == 'text')
    if 'DSML' in text or 'invoke name' in text:
        fail(wrapper, 'anthropic.stream.dsml', f'markup leaked into SDK-visible text: {text[:120]!r}')
        return
    if not any(b.name == 'get_weather' for b in tool_uses):
        fail(wrapper, 'anthropic.stream.dsml', f'no get_weather tool_use (got {[b.name for b in tool_uses]})')
        return
    w = next(b for b in tool_uses if b.name == 'get_weather')
    if not isinstance(w.input, dict) or w.input.get('city') != 'Jakarta':
        fail(wrapper, 'anthropic.stream.dsml', f'tool input not recovered: {w.input!r}')
        return
    if msg.stop_reason != 'tool_use':
        fail(wrapper, 'anthropic.stream.dsml', f'stop_reason={msg.stop_reason!r}, want tool_use')
        return
    ok(wrapper, 'anthropic.dsml_loop', 'recovered + executable')


def check_anthropic_nonstream_tools(wrapper: str, port: int):
    ac = _anthropic_client(port)
    try:
        msg = ac.messages.create(model='mock/tools', max_tokens=256, tools=_TOOLS_ANTHROPIC,
                                 messages=[{'role': 'user', 'content': 'call both tools'}])
    except Exception as e:
        fail(wrapper, 'anthropic.nonstream.tools', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    tool_uses = [b for b in _blocks(msg) if getattr(b, 'type', '') == 'tool_use']
    if len(tool_uses) != 2 or msg.stop_reason != 'tool_use':
        fail(wrapper, 'anthropic.nonstream.tools',
             f'{len(tool_uses)} tool_use, stop_reason={msg.stop_reason!r}')
        return
    ok(wrapper, 'anthropic.nonstream.tools', 'strict Message parse')


def check_anthropic_slow_stream(wrapper: str, port: int):
    """Slow upstream: wrapper heartbeats must keep strict SDK iteration alive
    (no read timeout, no parse error on comment frames)."""
    ac = _anthropic_client(port)
    try:
        with ac.messages.stream(model='mock/slow', max_tokens=128,
                                messages=[{'role': 'user', 'content': 'hi'}]) as s:
            msg = s.get_final_message()
        if msg.stop_reason not in ('end_turn', 'max_tokens'):
            fail(wrapper, 'anthropic.stream.slow', f'stop_reason={msg.stop_reason!r}')
            return
    except Exception as e:
        fail(wrapper, 'anthropic.stream.slow', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    ok(wrapper, 'anthropic.stream.slow', 'heartbeats OK')


def check_openai_chat_tool_loop(wrapper: str, port: int):
    oc = _openai_client(port)
    try:
        r = oc.chat.completions.create(model='mock/tools', messages=[{'role': 'user', 'content': 'call both'}],
                                       tools=_TOOLS_OPENAI)
    except Exception as e:
        fail(wrapper, 'openai.chat.tools', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    msg = r.choices[0].message
    if not msg.tool_calls or len(msg.tool_calls) != 2:
        fail(wrapper, 'openai.chat.tools', f'tool_calls={msg.tool_calls!r}')
        return
    for tc in msg.tool_calls:
        try:
            json.loads(tc.function.arguments or '')
        except ValueError:
            fail(wrapper, 'openai.chat.tools', f'arguments not JSON: {tc.function.arguments!r}')
            return
    convo = [{'role': 'user', 'content': 'call both'},
             {'role': 'assistant', 'content': msg.content, 'tool_calls': [
                 {'id': tc.id, 'type': 'function',
                  'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
                 for tc in msg.tool_calls]}]
    for tc in msg.tool_calls:
        convo.append({'role': 'tool', 'tool_call_id': tc.id, 'content': 'ok'})
    try:
        r2 = oc.chat.completions.create(model='mock/echo', messages=convo)
    except Exception as e:
        fail(wrapper, 'openai.chat.tool_turn', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    content = r2.choices[0].message.content or ''
    try:
        fwd = json.loads(content[content.index('{'):])
        ids = [m.get('tool_call_id') for m in fwd.get('messages', []) if m.get('role') == 'tool']
    except Exception as e:
        fail(wrapper, 'openai.chat.tool_turn', f'echo body unreadable: {str(e)[:160]}')
        return
    if sorted(ids) != sorted(tc.id for tc in msg.tool_calls):
        fail(wrapper, 'openai.chat.tool_turn', f'role:tool ids upstream={ids}')
        return
    ok(wrapper, 'openai.chat.tool_loop', 'turn1 tools → role:tool → turn2')


def check_openai_chat_stream_tools(wrapper: str, port: int):
    oc = _openai_client(port)
    try:
        names: dict[int, str] = {}
        args: dict[int, str] = {}
        finish = None
        for ch in oc.chat.completions.create(model='mock/tools', stream=True,
                                             messages=[{'role': 'user', 'content': 'call both'}],
                                             tools=_TOOLS_OPENAI):
            if not ch.choices:
                continue
            c0 = ch.choices[0]
            for tc in (c0.delta.tool_calls or []):
                if tc.function and tc.function.name:
                    names[tc.index] = names.get(tc.index, '') + tc.function.name
                if tc.function and tc.function.arguments:
                    args[tc.index] = args.get(tc.index, '') + tc.function.arguments
            if c0.finish_reason:
                finish = c0.finish_reason
    except Exception as e:
        fail(wrapper, 'openai.chat.stream.tools', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    if finish != 'tool_calls':
        fail(wrapper, 'openai.chat.stream.tools', f'finish_reason={finish!r}')
        return
    for i, nm in names.items():
        a = args.get(i, '')
        # The P0-3 class: the name must NEVER have been emitted inside the
        # arguments delta (accumulated args must be parseable JSON).
        try:
            obj = json.loads(a)
        except ValueError:
            fail(wrapper, 'openai.chat.stream.tools', f'accumulated args not JSON for {nm}: {a!r}')
            return
        if isinstance(obj, str) and obj.startswith(nm):
            fail(wrapper, 'openai.chat.stream.tools', f'name leaked into args for {nm}')
            return
    ok(wrapper, 'openai.chat.stream.tools', f'tools={sorted(names.values())}')


def check_responses_replay(wrapper: str, port: int):
    """Codex shape: turn 1 (tools) stored; turn 2 sends function_call_output
    items with previous_response_id. The upstream must receive BOTH the
    replayed assistant tool_calls AND matching role:tool entries — an orphan
    tool message is a 400 upstream and the agent dies mid-loop."""
    oc = _openai_client(port)
    try:
        r1 = oc.responses.create(model='mock/tools', input='call both tools')
    except Exception as e:
        fail(wrapper, 'responses.tools_turn1', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    fcs = [o for o in (r1.output or []) if getattr(o, 'type', '') == 'function_call']
    if len(fcs) != 2:
        fail(wrapper, 'responses.tools_turn1', f'{len(fcs)} function_call items, want 2')
        return
    inputs = [{'type': 'function_call_output', 'call_id': fc.call_id, 'output': 'ok'} for fc in fcs]
    try:
        r2 = oc.responses.create(model='mock/echo', input=inputs,
                                 previous_response_id=r1.id)
    except Exception as e:
        fail(wrapper, 'responses.replay', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    txt = ''
    for o in (r2.output or []):
        if getattr(o, 'type', '') == 'message':
            for c in (getattr(o, 'content', []) or []):
                txt += getattr(c, 'text', '') or ''
    try:
        fwd = json.loads(txt[txt.index('{'):])
        msgs = [m for m in fwd.get('messages', []) if isinstance(m, dict)]
    except Exception as e:
        fail(wrapper, 'responses.replay', f'echo body unreadable: {str(e)[:160]}')
        return
    asst_tools = [tc for m in msgs if m.get('role') == 'assistant'
                  for tc in (m.get('tool_calls') or [])]
    tool_msgs = {m.get('tool_call_id') for m in msgs if m.get('role') == 'tool'}
    want = {fc.call_id for fc in fcs}
    got_asst = {tc.get('id') for tc in asst_tools}
    if not want <= got_asst:
        fail(wrapper, 'responses.replay',
             f'replayed history lost assistant tool_calls: want {want}, got {got_asst}')
        return
    if not want <= tool_msgs:
        fail(wrapper, 'responses.replay',
             f'function_call_output not mapped to role:tool upstream: want {want}, got {tool_msgs}')
        return
    ok(wrapper, 'responses.replay', 'store + replay + orphan-free')


def _shaped_sdk_error(e) -> bool:
    """The wrapper may collapse a retried upstream failure into its own
    exhaustion error (multi-key pool, CONTRACT §5) — what agents need is a
    SHAPED error the SDK can parse into a typed exception with an actionable
    message, never a parse error, a hang, or a fabricated success."""
    body = getattr(e, 'body', None)
    if isinstance(body, dict):
        err = body.get('error', body)
        if isinstance(err, dict) and isinstance(err.get('message'), str) and err.get('message'):
            return True
    return 'mock 500' in str(e)  # upstream detail preserved is fine too


def check_responses_replay_streamed_turn(wrapper: str, port: int):
    """Codex-streaming shape: turn 1 STREAMED (response.completed), turn 2
    previous_response_id replay. The streamed turn must have been stored
    (request + assistant incl. tool_calls) or the replay orphans the
    function_call_output upstream (400 — the agent dies mid-loop)."""
    oc = _openai_client(port)
    try:
        resp_id = None
        fcs = []
        for ev in oc.responses.create(model='mock/tools', input='call both tools', stream=True):
            t = getattr(ev, 'type', '')
            if t == 'response.output_item.done':
                item = getattr(ev, 'item', None)
                if getattr(item, 'type', '') == 'function_call':
                    fcs.append(item)
            elif t == 'response.completed':
                resp_id = ev.response.id
    except Exception as e:
        fail(wrapper, 'responses.stream_turn1', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    if not resp_id or len(fcs) != 2:
        fail(wrapper, 'responses.stream_turn1', f'resp_id={resp_id!r}, {len(fcs)} function_call items')
        return
    inputs = [{'type': 'function_call_output', 'call_id': fc.call_id, 'output': 'ok'} for fc in fcs]
    try:
        r2 = oc.responses.create(model='mock/echo', input=inputs, previous_response_id=resp_id)
    except Exception as e:
        fail(wrapper, 'responses.replay_streamed', f'SDK raised: {type(e).__name__}: {str(e)[:300]}')
        return
    txt = ''
    for o in (r2.output or []):
        if getattr(o, 'type', '') == 'message':
            for c in (getattr(o, 'content', []) or []):
                txt += getattr(c, 'text', '') or ''
    try:
        fwd = json.loads(txt[txt.index('{'):])
        msgs = [m for m in fwd.get('messages', []) if isinstance(m, dict)]
    except Exception as e:
        fail(wrapper, 'responses.replay_streamed', f'echo body unreadable: {str(e)[:160]}')
        return
    asst_tools = [tc for m in msgs if m.get('role') == 'assistant'
                  for tc in (m.get('tool_calls') or [])]
    tool_msgs = {m.get('tool_call_id') for m in msgs if m.get('role') == 'tool'}
    want = {fc.call_id for fc in fcs}
    got_asst = {tc.get('id') for tc in asst_tools}
    if not want <= got_asst:
        fail(wrapper, 'responses.replay_streamed',
             f'streamed turn not fully stored: want asst tool ids {want}, got {got_asst}')
        return
    if not want <= tool_msgs:
        fail(wrapper, 'responses.replay_streamed',
             f'function_call_output not mapped to role:tool: want {want}, got {tool_msgs}')
        return
    ok(wrapper, 'responses.replay_streamed', 'streamed turn stored + orphan-free')


def check_transient_429_sdk_retry(wrapper: str, port: int):
    """Agents run with SDK retries enabled: a one-shot upstream 429 must be
    retried INSIDE the SDK (retriable status + shaped body) and succeed —
    if the wrapper answered 429 with an unshaped/wrong-status body, every
    transient blip would surface to the user as a failed turn."""
    import anthropic as _a
    ac = _a.Anthropic(base_url=f'http://127.0.0.1:{port}', api_key=TOKEN,
                      auth_token=TOKEN, timeout=30.0, max_retries=3)
    try:
        msg = ac.messages.create(model='mock/http429once', max_tokens=64,
                                 messages=[{'role': 'user', 'content': 'hi'}])
        if not _blocks(msg):
            fail(wrapper, 'anthropic.429once_retry', 'empty message after SDK retry')
            return
    except Exception as e:
        fail(wrapper, 'anthropic.429once_retry',
             f'SDK retry did not recover: {type(e).__name__}: {str(e)[:200]}')
        return
    import openai as _o
    oc = _o.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=TOKEN,
                   timeout=30.0, max_retries=3)
    try:
        r = oc.chat.completions.create(model='mock/http429once',
                                       messages=[{'role': 'user', 'content': 'hi'}])
        if not r.choices:
            fail(wrapper, 'openai.429once_retry', 'no choices after SDK retry')
            return
    except Exception as e:
        fail(wrapper, 'openai.429once_retry',
             f'SDK retry did not recover: {type(e).__name__}: {str(e)[:200]}')
        return
    ok(wrapper, 'sdk.429once_retry', 'transient 429 recovered invisibly')


def check_responses_tenant_isolation(wrapper: str, port: int):
    """CONTRACT §6.3: response store is tenant-namespaced — a different token
    must NOT replay another principal's history (agent/data leak class)."""
    import openai as _o
    oc_a = _o.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=TOKEN,
                     timeout=30.0, max_retries=0)
    try:
        r1 = oc_a.responses.create(model='mock/normal', input='tenant-A secret message-QX7')
    except Exception as e:
        fail(wrapper, 'responses.tenant_isolation', f' turn1 raised: {type(e).__name__}: {str(e)[:200]}')
        return
    oc_b = _o.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key='different-tenant-token-XYZ',
                     timeout=30.0, max_retries=0)
    try:
        r2 = oc_b.responses.create(model='mock/echo', input='continue',
                                   previous_response_id=r1.id)
    except Exception as e:
        # B replaying A's id must not leak; an auth/store error is acceptable.
        ok(wrapper, 'responses.tenant_isolation', f'cross replay rejected ({type(e).__name__})')
        return
    txt = ''
    for o in (r2.output or []):
        if getattr(o, 'type', '') == 'message':
            for c in (getattr(o, 'content', []) or []):
                txt += getattr(c, 'text', '') or ''
    if 'secret message-QX7' in txt:
        fail(wrapper, 'responses.tenant_isolation',
             'TENANT LEAK: token B replayed token A history')
        return
    ok(wrapper, 'responses.tenant_isolation', 'no cross-tenant history')


def check_error_surface(wrapper: str, port: int):
    ac = _anthropic_client(port)
    try:
        ac.messages.create(model='mock/http500', max_tokens=64,
                           messages=[{'role': 'user', 'content': 'hi'}])
        fail(wrapper, 'anthropic.error_surface', 'http500 surfaced as success')
        return
    except Exception as e:
        if not _shaped_sdk_error(e):
            fail(wrapper, 'anthropic.error_surface',
                 f'unshaped error (agent shows garbage): {type(e).__name__}: {str(e)[:200]} body={str(getattr(e, "body", None))[:200]}')
            return
    oc = _openai_client(port)
    try:
        oc.chat.completions.create(model='mock/http500', messages=[{'role': 'user', 'content': 'hi'}])
        fail(wrapper, 'openai.error_surface', 'http500 surfaced as success')
        return
    except Exception as e:
        if not _shaped_sdk_error(e):
            fail(wrapper, 'openai.error_surface',
                 f'unshaped error: {type(e).__name__}: {str(e)[:200]} body={str(getattr(e, "body", None))[:200]}')
            return
    ok(wrapper, 'error_surface', '500 → shaped SDK exception')


CHECKS_MATRIX = [
    check_anthropic_tool_loop_stream,
    check_anthropic_dsml_loop,
    check_anthropic_nonstream_tools,
    check_anthropic_slow_stream,
    check_openai_chat_tool_loop,
    check_openai_chat_stream_tools,
    check_responses_replay,
    check_responses_replay_streamed_turn,
    check_transient_429_sdk_retry,
    check_responses_tenant_isolation,
    check_error_surface,
]


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrapper')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    VERBOSE = args.verbose
    targets = ({args.wrapper: WRAPPERS[args.wrapper]} if args.wrapper else WRAPPERS)

    mock = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/e2e_runtime/mock_upstream.py'), str(MOCK_PORT)],
        stdout=open('/tmp/al-mock.log', 'w'), stderr=subprocess.STDOUT)
    if not free_port_wait(MOCK_PORT):
        print('FATAL: mock upstream did not start')
        mock.kill()
        return 2
    print(f'mock upstream up on :{MOCK_PORT}\n')

    rc = 0
    try:
        for name, (wdir, port, upstream_var, extra) in targets.items():
            print(f'── {name} ' + '─' * (58 - len(name)))
            proc, logf = start_wrapper(name, wdir, port, upstream_var, extra)
            try:
                if not free_port_wait(port) or not health_wait(port):
                    tail = open(f'/tmp/al-{name}.log', errors='replace').read()[-1500:]
                    fail(name, 'boot', f'did not become healthy.\n{tail}')
                    continue
                for fn in CHECKS_MATRIX:
                    try:
                        fn(name, port)
                    except Exception as e:  # harness-side bug isolation
                        fail(name, fn.__name__, f'harness: {type(e).__name__}: {e}')
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
        for f_ in FAILURES:
            print(f'  ✗ {f_}')
        rc = 1
    else:
        print('\n✅ every agent loop works end to end on every wrapper (real SDKs)')
    return rc


if __name__ == '__main__':
    sys.exit(main())
