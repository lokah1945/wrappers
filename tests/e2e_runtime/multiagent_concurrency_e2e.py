#!/usr/bin/env python3
"""Multi-agent CONCURRENCY E2E — the round-7 gate.

The runtime/agent-loop gates proved correctness for sequential traffic. This
harness proves the wrapper holds up when MANY agents hit it simultaneously —
the production reality (several Claude Code / Codex / openclaw / hermes /
opencode / openhands sessions on one backend).

Every "agent" is a real anthropic/openai SDK client living in its own thread;
all agents of one wrapper run concurrently against the mock upstream and mix
surfaces + fault modes. Assertions:

  * NO CROSS-TALK — every agent flow embeds a unique marker; any foreign
    marker reaching its stream/replay = shared-state leakage (fail),
  * event integrity under load (SDK-strict parsing, tool args valid JSON,
    DSML recovery intact per request),
  * exactly-once in-flight accounting (CONTRACT §2.2.4): after the storm,
    /health MUST report in-flight counts and they MUST all be zero — a leaked
    reservation under concurrency eventually starves the key pool,
  * the response store stays consistent under concurrent same-principal
    writes/replays (§6.3),
  * intentional fault modes (transient 429, mid-stream abort) surface as
    SHAPED errors, never hang, never corrupt their neighbours,
  * no 5xx envelopes, no server-log tracebacks.

Usage:  python tests/e2e_runtime/multiagent_concurrency_e2e.py [--wrapper NAME] [-v]
Exit code 0 = race-free concurrent service on every wrapper.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

AGENTS = 12         # concurrent agents per wrapper
ROUNDS = 3          # rounds per agent (each round = one full agent flow)

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

    logf = open(f'/tmp/mc-{name}.log', 'w')
    p = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.main:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT / wdir), env=env, stdout=logf, stderr=subprocess.STDOUT)
    return p, logf


def scan_log(name: str) -> list[str]:
    path = f'/tmp/mc-{name}.log'
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


# ── per-agent flows (each returns a list of problems) ──────────────────────

_TOOLS_A = [{'name': 'alpha', 'description': 'a', 'input_schema': {'type': 'object', 'properties': {}}}]
_TOOLS_O = [{'type': 'function', 'function': {'name': 'alpha', 'description': 'a',
                                              'parameters': {'type': 'object', 'properties': {}}}}]


def _foreign_markers(text: str, my_marker: str) -> list[str]:
    """Any OTHER agent's marker in MY traffic = shared-state cross-talk."""
    out = []
    for i in range(AGENTS):
        m = f'MARK{i}-QX7'
        if m != my_marker and m in text:
            out.append(m)
    return out


def flow_anthropic(port: int, agent: int, rnd: int) -> list[str]:
    """Stream tools turn + tool_result echo turn — cross-talk visible in echo."""
    import anthropic
    marker = f'MARK{agent}-QX7'
    ac = anthropic.Anthropic(base_url=f'http://127.0.0.1:{port}', api_key=TOKEN,
                             auth_token=TOKEN, timeout=45.0, max_retries=2)
    problems = []
    try:
        with ac.messages.stream(model='mock/tools', max_tokens=512, tools=_TOOLS_A,
                                messages=[{'role': 'user', 'content': f'{marker} call alpha'}]) as s:
            msg = s.get_final_message()
        tool_uses = [b for b in msg.content if getattr(b, 'type', '') == 'tool_use']
        if msg.stop_reason != 'tool_use' or not tool_uses:
            problems.append(f'tool turn bad: stop={msg.stop_reason!r} tools={len(tool_uses)}')
            return problems
        convo = [
            {'role': 'user', 'content': f'{marker} call alpha'},
            {'role': 'assistant', 'content': [dict(b.model_dump()) for b in msg.content]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': b.id, 'content': f'{marker} ok'} for b in tool_uses]},
        ]
        m2 = ac.messages.create(model='mock/echo', max_tokens=8192, messages=convo)
        echo = ''.join(getattr(b, 'text', '') for b in m2.content
                       if getattr(b, 'type', '') == 'text')
        if marker not in echo:
            problems.append('my marker missing from echoed upstream body')
        fm = _foreign_markers(echo, marker)
        if fm:
            problems.append(f'CROSS-TALK: foreign markers {fm} in my echo')
        try:
            fwd = json.loads(echo[echo.index('{'):])
            tool_msgs = {m.get('tool_call_id') for m in fwd.get('messages', []) if m.get('role') == 'tool'}
            if not {b.id for b in tool_uses} <= tool_msgs:
                problems.append(f'orphan tool_result upstream (ids {sorted(tool_msgs)})')
        except Exception as e:
            problems.append(f'echo unreadable: {e}')
    except Exception as e:
        problems.append(f'{type(e).__name__}: {str(e)[:200]}')
    return problems


def flow_anthropic_dsml(port: int, agent: int, rnd: int) -> list[str]:
    import anthropic
    ac = anthropic.Anthropic(base_url=f'http://127.0.0.1:{port}', api_key=TOKEN,
                             auth_token=TOKEN, timeout=45.0, max_retries=2)
    problems = []
    try:
        with ac.messages.stream(
                model='mock/dsml_stream', max_tokens=256,
                tools=[{'name': 'get_weather', 'description': 'w',
                        'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}],
                messages=[{'role': 'user', 'content': 'weather?'}]) as s:
            msg = s.get_final_message()
        tools = [b for b in msg.content if getattr(b, 'type', '') == 'tool_use']
        text = ''.join(getattr(b, 'text', '') for b in msg.content if getattr(b, 'type', '') == 'text')
        if 'DSML' in text or 'invoke name' in text:
            problems.append(f'DSML markup in SDK-visible text: {text[:100]!r}')
        if not any(b.name == 'get_weather' for b in tools) or msg.stop_reason != 'tool_use':
            problems.append(f'DSML not recovered under load: tools={[b.name for b in tools]} stop={msg.stop_reason!r}')
    except Exception as e:
        problems.append(f'{type(e).__name__}: {str(e)[:200]}')
    return problems


def flow_openai_tools(port: int, agent: int, rnd: int) -> list[str]:
    import openai
    oc = openai.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=TOKEN,
                       timeout=45.0, max_retries=2)
    problems = []
    try:
        names = {}
        args = {}
        for ch in oc.chat.completions.create(model='mock/tools', stream=True,
                                             messages=[{'role': 'user', 'content': 'hi'}],
                                             tools=_TOOLS_O):
            if not ch.choices:
                continue
            for tc in (ch.choices[0].delta.tool_calls or []):
                if tc.function and tc.function.name:
                    names[tc.index] = names.get(tc.index, '') + tc.function.name
                if tc.function and tc.function.arguments:
                    args[tc.index] = args.get(tc.index, '') + tc.function.arguments
        for i, nm in names.items():
            try:
                json.loads(args.get(i, ''))
            except ValueError:
                problems.append(f'accumulated args not JSON for {nm}: {args.get(i)!r}')
    except Exception as e:
        problems.append(f'{type(e).__name__}: {str(e)[:200]}')
    return problems


def flow_responses_replay(port: int, agent: int, rnd: int) -> list[str]:
    import openai
    marker = f'MARK{agent}-QX7'
    oc = openai.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=TOKEN,
                       timeout=45.0, max_retries=2)
    problems = []
    try:
        r1 = oc.responses.create(model='mock/tools', input=f'{marker} call both tools')
        fcs = [o for o in (r1.output or []) if getattr(o, 'type', '') == 'function_call']
        if len(fcs) != 2:
            problems.append(f'{len(fcs)} function_call items')
            return problems
        r2 = oc.responses.create(
            model='mock/echo', previous_response_id=r1.id,
            input=[{'type': 'function_call_output', 'call_id': fc.call_id, 'output': f'{marker} ok'} for fc in fcs])
        txt = ''
        for o in (r2.output or []):
            if getattr(o, 'type', '') == 'message':
                for c in (getattr(o, 'content', []) or []):
                    txt += getattr(c, 'text', '') or ''
        if marker not in txt:
            problems.append('my marker missing from replayed echo')
        fm = _foreign_markers(txt, marker)
        if fm:
            problems.append(f'CROSS-TALK: foreign markers {fm} in my replay')
        try:
            fwd = json.loads(txt[txt.index('{'):])
            msgs = [m for m in fwd.get('messages', []) if isinstance(m, dict)]
            asst = {tc.get('id') for m in msgs if m.get('role') == 'assistant'
                    for tc in (m.get('tool_calls') or [])}
            toolm = {m.get('tool_call_id') for m in msgs if m.get('role') == 'tool'}
            want = {fc.call_id for fc in fcs}
            if not want <= asst or not want <= toolm:
                problems.append(f'replay orphan: asst={sorted(asst)} tool={sorted(toolm)}')
        except Exception as e:
            problems.append(f'echo unreadable: {e}')
    except Exception as e:
        problems.append(f'{type(e).__name__}: {str(e)[:200]}')
    return problems


def flow_slow_stream(port: int, agent: int, rnd: int) -> list[str]:
    import anthropic
    ac = anthropic.Anthropic(base_url=f'http://127.0.0.1:{port}', api_key=TOKEN,
                             auth_token=TOKEN, timeout=60.0, max_retries=1)
    try:
        with ac.messages.stream(model='mock/slow', max_tokens=128,
                                messages=[{'role': 'user', 'content': 'hi'}]) as s:
            s.get_final_message()
    except Exception as e:
        return [f'{type(e).__name__}: {str(e)[:200]}']
    return []


def flow_transient_error(port: int, agent: int, rnd: int) -> list[str]:
    """429-once must be retried invisibly by the SDK (max_retries=3)."""
    import openai
    oc = openai.OpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=TOKEN,
                       timeout=45.0, max_retries=3)
    try:
        r = oc.chat.completions.create(model='mock/http429once',
                                       messages=[{'role': 'user', 'content': 'hi'}])
        if not r.choices:
            return ['no choices after transient-429 retry']
    except Exception as e:
        return [f'transient 429 not recovered: {type(e).__name__}: {str(e)[:200]}']
    return []


def flow_abrupt_stream(port: int, agent: int, rnd: int) -> list[str]:
    """Mid-stream abort: SDK MUST surface an error (never a clean truncated
    success), and the HTTP layer must not turn 5xx."""
    import anthropic
    ac = anthropic.Anthropic(base_url=f'http://127.0.0.1:{port}', api_key=TOKEN,
                             auth_token=TOKEN, timeout=45.0, max_retries=0)
    try:
        with ac.messages.stream(model='mock/abrupt', max_tokens=128,
                                messages=[{'role': 'user', 'content': 'hi'}]) as s:
            s.get_final_message()
        return ['abrupt surfaced as a clean success (truncation masquerading)']
    except Exception:
        return []  # an exception is the CORRECT surfaced failure


FLOWS = [
    flow_anthropic,
    flow_anthropic_dsml,
    flow_openai_tools,
    flow_responses_replay,
    flow_slow_stream,
    flow_transient_error,
    flow_abrupt_stream,
]


def run_agent(wrapper: str, port: int, agent: int) -> list[str]:
    problems = []
    for rnd in range(ROUNDS):
        fn = FLOWS[(agent + rnd) % len(FLOWS)]
        try:
            for p in fn(port, agent, rnd):
                problems.append(f'agent{agent} round{rnd} {fn.__name__}: {p}')
        except Exception as e:
            problems.append(f'agent{agent} round{rnd} {fn.__name__}: harness {type(e).__name__}: {e}')
    return problems


# ── post-storm invariants ──────────────────────────────────────────────────

def _find_in_flight(obj, path=''):
    """All (path, value) pairs named *in_flight* anywhere in a JSON tree."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if 'in_flight' in str(k).lower() and isinstance(v, (int, float)):
                out.append((f'{path}.{k}', v))
            else:
                out.extend(_find_in_flight(v, f'{path}.{k}'))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_find_in_flight(v, f'{path}[{i}]'))
    return out


def check_post_storm(wrapper: str, port: int):
    """CONTRACT §2.2.4/§10: after the storm every reservation is released —
    /health must REPORT in-flight counts and they must all be zero. Also no
    leaked reservations visible via /metrics."""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=10) as r:
            health = json.loads(r.read())
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=10) as r:
            metrics = json.loads(r.read())
    except Exception as e:
        fail(wrapper, 'post_storm', f'health/metrics unreadable: {e}')
        return
    spots = _find_in_flight(health) + _find_in_flight(metrics)
    nonzero = [(p, v) for p, v in spots if v]
    if nonzero:
        fail(wrapper, 'post_storm',
             f'leaked in-flight reservations after storm: {nonzero[:6]} '
             '(pool starvation class — CONTRACT §2.2.4 exactly-once)')
        return
    if not spots:
        fail(wrapper, 'post_storm',
             'no in-flight counts reported by /health or /metrics (CONTRACT §10 '
             'requires /health to report in-flight counts)')
        return
    ok(wrapper, 'post_storm', f'in-flight zero at {len(spots)} counters')


# ── driver ─────────────────────────────────────────────────────────────────

def storm(wrapper: str, port: int):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=AGENTS) as ex:
        futures = [ex.submit(run_agent, wrapper, port, a) for a in range(AGENTS)]
        all_problems = []
        for f in futures:
            all_problems.extend(f.result())
    dt = time.time() - t0
    if all_problems:
        for p in all_problems[:14]:
            fail(wrapper, 'concurrency', p)
        if len(all_problems) > 14:
            fail(wrapper, 'concurrency', f'…and {len(all_problems) - 14} more problems')
        return
    ok(wrapper, 'concurrency', f'{AGENTS} agents × {ROUNDS} rounds in {dt:.1f}s — race-free')


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
        stdout=open('/tmp/mc-mock.log', 'w'), stderr=subprocess.STDOUT)
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
                    tail = open(f'/tmp/mc-{name}.log', errors='replace').read()[-1500:]
                    fail(name, 'boot', f'did not become healthy.\n{tail}')
                    continue
                storm(name, port)
                # let the last streams fully unwind before reading counters
                time.sleep(1.0)
                check_post_storm(name, port)
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
        print('\n✅ race-free concurrent service on every wrapper (multi-agent storm)')
    return rc


if __name__ == '__main__':
    sys.exit(main())
