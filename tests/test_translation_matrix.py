#!/usr/bin/env python3
"""AI Gateway Translation Layer matrix — every wrapper's Anthropic↔OpenAI and
Responses↔Chat converters must round-trip real client payloads without loss.

This is the "API Translation Layer / Compatibility Layer / AI Gateway
Protocol" gate: a Claude Code / Hermes / OpenClaw / generic Anthropic SDK
request reaching any wrapper's Anthropic surface must be translated to the
OpenAI upstream and back WITHOUT dropping system, images, reasoning, tools,
tool_choice, stop_sequences, or usage; a Codex / OpenAI SDK request on the
Responses surface must survive the Responses→Chat→Responses round trip; and
OpenAI→OpenAI / Anthropic→Anthropic flows must pass through without mutation.

Parity gaps found by this suite (fixed 2026-08-01):
  - nous: dropped tool_choice, broke URL-source images, dropped reasoning in
    chat_to_responses, missing usage.total_tokens, dropped output_text items
    in responses_to_chat, str() repr of function_call_output
  - opencode: dropped stop_sequences, forwarded Anthropic tool_choice shape
    verbatim (upstream 400/ignore)
  - openrouter: dropped thinking in request+response translation, dropped
    URL-source images, no thinking blocks in streaming Anthropic surface
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WRAPPERS = ('nous', 'opencode', 'blackbox', 'openrouter')


def _load(wname):
    import importlib
    return importlib.import_module(f'{wname}.src.main')


def _anthropic_to_openai_fn(m, wname):
    return m.anthropic_to_openai if wname != 'openrouter' else m._anthropic_to_openai


# ── Anthropic request → OpenAI chat ────────────────────────────────────────

ANTHRO_REQ = {
    "model": "mock/default",
    "max_tokens": 1024,
    "system": [{"type": "text", "text": "You are helpful."}],
    "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
            {"type": "text", "text": "What is this?"},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me analyze."},
            {"type": "text", "text": "I see an image."},
            {"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {"key": "val"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01", "content": "found it"},
            {"type": "text", "text": "Now answer."},
        ]},
    ],
    "tools": [{"name": "lookup", "description": "Look things up",
               "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}}}],
    "tool_choice": {"type": "auto"},
    "stop_sequences": ["END"],
    "temperature": 0.7,
}


@pytest.mark.parametrize('wname', WRAPPERS)
def test_anthropic_request_to_openai_full_roundtrip(wname):
    m = _load(wname)
    fn = _anthropic_to_openai_fn(m, wname)
    out = fn(json.loads(json.dumps(ANTHRO_REQ)))
    if isinstance(out, dict) and out.get('error'):
        pytest.fail(f'{wname} returned error: {out}')
    msgs = out.get('messages', [])
    roles = [x.get('role') for x in msgs]
    assert roles and roles[0] == 'system', f'{wname}: no system message: {roles}'
    assert 'tool' in roles, f'{wname}: tool_result not mapped to tool role: {roles}'
    assert any(x.get('reasoning_content') for x in msgs if x.get('role') == 'assistant'), \
        f'{wname}: thinking block lost (no reasoning_content)'
    assert any(x.get('tool_calls') for x in msgs if x.get('role') == 'assistant'), \
        f'{wname}: tool_use not mapped to tool_calls'
    img_msg = next((x for x in msgs if x.get('role') == 'user'), None)
    content = img_msg.get('content') if img_msg else None
    parts = content if isinstance(content, list) else [{'text': content or ''}]
    assert any(p.get('type') == 'image_url' and p.get('image_url', {}).get('url', '').startswith('data:image/png;base64,')
               for p in parts if isinstance(p, dict)), f'{wname}: base64 image lost: {content!r}'
    assert out.get('stop') == ['END'], f'{wname}: stop_sequences not mapped: {out.get("stop")!r}'
    tc = out.get('tool_choice')
    assert tc in ('auto', 'required', 'none') or (isinstance(tc, dict) and tc.get('type') == 'function'), \
        f'{wname}: tool_choice not mapped: {tc!r}'
    tools = out.get('tools') or []
    assert tools and tools[0].get('function', {}).get('parameters', {}).get('type') == 'object', \
        f'{wname}: tools shape wrong: {tools!r}'


@pytest.mark.parametrize('wname', WRAPPERS)
def test_anthropic_single_image_message_does_not_crash(wname):
    """A user message containing exactly ONE image block (no text) previously
    crashed nous and blackbox with KeyError: 'text' (indexing parts[0]['text']
    on an image_url part). The content must be wrapped as an array, never a
    bare dict (OpenAI chat requires string or array)."""
    m = _load(wname)
    fn = _anthropic_to_openai_fn(m, wname)
    req = {"model": "mock/default", "max_tokens": 64, "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]}
    out = fn(req)
    msgs = out.get('messages', [])
    assert msgs, f'{wname}: no messages'
    c = msgs[0].get('content')
    assert isinstance(c, list) and c and all(isinstance(p, dict) for p in c), \
        f'{wname}: content must be an array of parts: {c!r}'
    assert c[0].get('type') == 'image_url', f'{wname}: {c!r}'


@pytest.mark.parametrize('wname', WRAPPERS)
def test_anthropic_url_image_passthrough(wname):
    m = _load(wname)
    fn = _anthropic_to_openai_fn(m, wname)
    req = json.loads(json.dumps(ANTHRO_REQ))
    req['messages'] = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
        {"type": "text", "text": "what?"}]}]
    out = fn(req)
    msgs = out.get('messages', [])
    img_msg = next((x for x in msgs if x.get('role') == 'user'), None)
    parts = img_msg.get('content') if isinstance(img_msg.get('content'), list) else [img_msg]
    urls = [p.get('image_url', {}).get('url', '') for p in parts if isinstance(p, dict) and p.get('type') == 'image_url']
    assert 'https://example.com/a.png' in urls, \
        f'{wname}: URL image not passed through: {urls!r}'
    # A URL must never be crammed into a base64 data URI.
    assert not any(u.startswith('data:') and 'base64,https://' in u for u in urls)


@pytest.mark.parametrize('wname', WRAPPERS)
@pytest.mark.parametrize('tc,expected', [
    ({"type": "any"}, 'required'),
    ({"type": "tool", "name": "lookup"}, 'function'),
    ({"type": "auto"}, 'auto'),
])
def test_anthropic_tool_choice_mapping(wname, tc, expected):
    m = _load(wname)
    fn = _anthropic_to_openai_fn(m, wname)
    req = json.loads(json.dumps(ANTHRO_REQ))
    req['tool_choice'] = tc
    out = fn(req)
    got = out.get('tool_choice')
    if expected == 'required':
        assert got == 'required', f'{wname}: {{any}} → {got!r}'
    elif expected == 'auto':
        assert got == 'auto', f'{wname}: {{auto}} → {got!r}'
    else:
        assert isinstance(got, dict) and got.get('type') == 'function' \
            and got.get('function', {}).get('name') == 'lookup', \
            f'{wname}: {{tool}} → {got!r}'


# ── OpenAI chat response → Anthropic response ─────────────────────────────

OAI_RESP = {
    "id": "chatcmpl-abc", "object": "chat.completion", "model": "mock/default",
    "choices": [{"index": 0, "message": {
        "role": "assistant", "content": "Hello!",
        "reasoning_content": "Internal thought",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "lookup", "arguments": '{"key":"val"}'}}]},
        "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _openai_to_anthropic_fn(m, wname):
    if wname == 'openrouter':
        return lambda data: m._openai_to_anthropic_response(data, {"model": "mock/default"})
    return lambda data: m.openai_to_anthropic('mock/default', data)


@pytest.mark.parametrize('wname', WRAPPERS)
def test_openai_response_to_anthropic_full_roundtrip(wname):
    m = _load(wname)
    fn = _openai_to_anthropic_fn(m, wname)
    out = fn(json.loads(json.dumps(OAI_RESP)))
    assert out.get('type') == 'message', f'{wname}: not a message: {str(out)[:120]}'
    types = [c.get('type') for c in out.get('content', [])]
    assert 'thinking' in types, f'{wname}: reasoning lost: {types}'
    assert 'text' in types, f'{wname}: text lost: {types}'
    assert 'tool_use' in types, f'{wname}: tool_calls lost: {types}'
    tu = next(c for c in out['content'] if c.get('type') == 'tool_use')
    assert tu.get('input') == {'key': 'val'}, f'{wname}: tool args not parsed: {tu}'
    assert out.get('stop_reason') == 'tool_use', f'{wname}: {out.get("stop_reason")}'
    assert out.get('usage', {}).get('input_tokens') == 10, f'{wname}: usage lost'


@pytest.mark.parametrize('wname', WRAPPERS)
@pytest.mark.parametrize('fr,expected', [
    ('stop', 'end_turn'), ('length', 'max_tokens'),
    ('tool_calls', 'tool_use'), ('content_filter', 'refusal'),
])
def test_openai_finish_reason_strict_mapping(wname, fr, expected):
    m = _load(wname)
    fn = _openai_to_anthropic_fn(m, wname)
    resp = json.loads(json.dumps(OAI_RESP))
    resp['choices'][0]['finish_reason'] = fr
    if fr != 'tool_calls':
        resp['choices'][0]['message']['tool_calls'] = []
    out = fn(resp)
    assert out.get('stop_reason') == expected, \
        f'{wname}: finish {fr} → {out.get("stop_reason")!r}, expected {expected}'


# ── Responses request → chat ──────────────────────────────────────────────

RESP_REQ = {
    "model": "mock/default",
    "instructions": "Be concise.",
    "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"x":1}'},
        {"type": "function_call_output", "call_id": "call_1", "output": {"result": 42}},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
    ],
    "max_output_tokens": 512,
    "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}],
}


@pytest.mark.parametrize('wname', WRAPPERS)
def test_responses_request_to_chat_full_roundtrip(wname):
    m = _load(wname)
    out = m.responses_to_chat(json.loads(json.dumps(RESP_REQ)), 'test-principal')
    msgs = out.get('messages', [])
    roles = [x.get('role') for x in msgs]
    assert roles and roles[0] == 'system', f'{wname}: instructions lost: {roles}'
    # assistant output_text from a previous turn must survive (conversation
    # continuity for Codex multi-turn input arrays).
    assert 'assistant' in roles, f'{wname}: assistant output_text dropped: {roles}'
    assert any(x.get('tool_calls') for x in msgs if x.get('role') == 'assistant'), \
        f'{wname}: function_call lost'
    assert 'tool' in roles, f'{wname}: function_call_output lost: {roles}'
    tool_msg = next(x for x in msgs if x.get('role') == 'tool')
    # dict output must serialize as JSON, never a Python repr
    assert '{"result": 42}' in tool_msg.get('content', ''), \
        f'{wname}: function_call_output not JSON: {tool_msg.get("content")!r}'
    assert out.get('max_tokens') == 512, f'{wname}: max_output_tokens lost'
    tools = out.get('tools') or []
    assert tools and tools[0].get('function', {}).get('name') == 'lookup', \
        f'{wname}: tools lost: {tools!r}'


REASON_INPUT = {
    "model": "mock/default",
    "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "reasoning", "id": "rs_1",
         "summary": [{"type": "summary_text", "text": "thinking summary"}],
         "content": [{"type": "reasoning_text", "text": "full reasoning"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
    ],
}


@pytest.mark.parametrize('wname', WRAPPERS)
def test_responses_reasoning_input_items_skipped_not_emptied(wname):
    """Codex multi-turn input arrays include `{"type":"reasoning",...}` items.
    They must be skipped — never converted into an empty user message, which
    upstream OpenAI APIs reject with 400 (content must not be empty)."""
    m = _load(wname)
    out = m.responses_to_chat(json.loads(json.dumps(REASON_INPUT)), 'p')
    msgs = out.get('messages', [])
    empties = [x for x in msgs if x.get('content') in ('', []) and not x.get('tool_calls')]
    assert not empties, f'{wname}: reasoning item became an empty message: {empties}'
    assert len(msgs) == 2, f'{wname}: expected 2 messages, got {len(msgs)}: {msgs}'


def test_nvidia_responses_reasoning_input_items_skipped():
    rc = _nvidia_import('responses_compat')
    msgs = rc.input_to_messages(json.loads(json.dumps(REASON_INPUT))['input'])
    empties = [x for x in msgs if x.get('content') in ('', []) and not x.get('tool_calls')]
    assert not empties, f'nvidia: reasoning item became an empty message: {empties}'
    assert len(msgs) == 2, f'nvidia: expected 2 messages, got {len(msgs)}'


# ── Chat response → Responses ─────────────────────────────────────────────

CHAT_RESP = {
    "id": "chatcmpl-xyz", "object": "chat.completion", "model": "mock/default",
    "choices": [{"index": 0, "message": {
        "role": "assistant", "content": "Sure!",
        "reasoning_content": "hmm",
        "tool_calls": [{"id": "call_9", "type": "function",
                        "function": {"name": "f", "arguments": '{"a":1}'}}]},
        "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


@pytest.mark.parametrize('wname', WRAPPERS)
def test_chat_response_to_responses_full_roundtrip(wname):
    m = _load(wname)
    out = m.chat_to_responses('mock/default', json.loads(json.dumps(CHAT_RESP)))
    assert out.get('object') == 'response', f'{wname}: {str(out)[:80]}'
    assert out.get('status') == 'completed', f'{wname}: {out.get("status")}'
    otypes = [o.get('type') for o in out.get('output', [])]
    assert 'reasoning' in otypes, f'{wname}: reasoning item lost: {otypes}'
    assert 'function_call' in otypes, f'{wname}: tool_calls lost: {otypes}'
    assert 'message' in otypes, f'{wname}: message lost: {otypes}'
    usage = out.get('usage') or {}
    assert usage.get('total_tokens') is not None, \
        f'{wname}: usage.total_tokens missing (Codex SDK requires it): {usage}'
    assert usage.get('input_tokens') == 3, f'{wname}: usage.input_tokens wrong: {usage}'


# ── nvidia-python (own modules: anthropic_compat / responses_compat) ──────

def _nvidia_import(modname):
    """Import an nvidia-python submodule under a private package anchor so its
    relative imports (`.capabilities`, `.anthropic_compat`) resolve against
    nvidia-python/src regardless of whatever else has imported a `src`
    namespace in this process (the directory name has a hyphen, so it cannot
    be imported as `nvidia_python`)."""
    import importlib
    import types
    pkg_name = '_nvidia_src'
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT / 'nvidia-python' / 'src')]
        sys.modules[pkg_name] = pkg
    return importlib.import_module(f'{pkg_name}.{modname}')


def _nvidia_anthro_to_openai(body):
    mod = _nvidia_import('anthropic_compat')
    return mod.anthropic_to_openai(json.loads(json.dumps(body)))


def _nvidia_openai_to_anthropic(data):
    mod = _nvidia_import('anthropic_compat')
    return mod.openai_to_anthropic(json.loads(json.dumps(data)), 'mock/default')


def _nvidia_responses_to_chat(body):
    rc = _nvidia_import('responses_compat')
    msgs = rc.input_to_messages(body.get('input'), body.get('instructions'))
    out = {'model': body.get('model', ''), 'messages': msgs,
           'stream': bool(body.get('stream', False))}
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = body['max_output_tokens']
    tools = rc.convert_tools(body.get('tools'))
    if tools:
        out['tools'] = tools
    return out


def _nvidia_chat_to_responses(data):
    rc = _nvidia_import('responses_compat')
    return rc.respond_non_streaming(json.loads(json.dumps(data)), 'mock/default')


def test_nvidia_anthropic_request_to_openai_full_roundtrip():
    out = _nvidia_anthro_to_openai(ANTHRO_REQ)
    msgs = out.get('messages', [])
    roles = [x.get('role') for x in msgs]
    assert roles and roles[0] == 'system', f'nvidia: {roles}'
    assert 'tool' in roles, f'nvidia: tool_result lost: {roles}'
    assert any(x.get('reasoning_content') for x in msgs if x.get('role') == 'assistant'), 'nvidia: thinking lost'
    assert any(x.get('tool_calls') for x in msgs if x.get('role') == 'assistant'), 'nvidia: tool_use lost'
    assert out.get('stop') == ['END'], f'nvidia: stop_sequences: {out.get("stop")!r}'
    img_msg = next((x for x in msgs if x.get('role') == 'user'), None)
    parts = img_msg.get('content') if isinstance(img_msg.get('content'), list) else []
    assert any(p.get('type') == 'image_url' and p.get('image_url', {}).get('url', '').startswith('data:image/png;base64,')
               for p in parts if isinstance(p, dict)), 'nvidia: base64 image lost'


def test_nvidia_openai_response_to_anthropic_full_roundtrip():
    out = _nvidia_openai_to_anthropic(OAI_RESP)
    types = [c.get('type') for c in out.get('content', [])]
    assert 'thinking' in types and 'tool_use' in types, f'nvidia: {types}'
    tu = next(c for c in out['content'] if c.get('type') == 'tool_use')
    assert tu.get('input') == {'key': 'val'}, 'nvidia: tool args not parsed'
    assert out.get('stop_reason') == 'tool_use', f'nvidia: {out.get("stop_reason")}'


def test_nvidia_responses_to_chat_full_roundtrip():
    out = _nvidia_responses_to_chat(RESP_REQ)
    roles = [x.get('role') for x in out['messages']]
    assert roles and roles[0] == 'system', f'nvidia: {roles}'
    assert 'assistant' in roles and 'tool' in roles, f'nvidia: {roles}'
    assert any(x.get('tool_calls') for x in out['messages'] if x.get('role') == 'assistant')
    assert out.get('max_tokens') == 512
    assert out.get('tools') and out['tools'][0]['function']['name'] == 'lookup'


def test_nvidia_chat_to_responses_full_roundtrip():
    out = _nvidia_chat_to_responses(CHAT_RESP)
    otypes = [o.get('type') for o in out.get('output', [])]
    assert 'reasoning' in otypes and 'function_call' in otypes and 'message' in otypes, \
        f'nvidia: {otypes}'
    assert out.get('usage', {}).get('total_tokens') == 7, f'nvidia: {out.get("usage")}'


# ── CODEX-RESP-02 — Responses streaming must parse with the openai SDK ─────
#
# The 2026-08-01 deep audit found the last Codex bug: ALL five wrappers
# emitted a Responses stream the openai SDK (the parser Codex uses) cannot
# handle:
#   1. response.created carried a minimal {id, model, status} — the SDK
#      snapshot's `output` was None, so the FIRST output_item.added crashed
#      with AttributeError: 'NoneType' object has no attribute 'append'.
#   2. tool arguments were streamed as response.function_call.delta — the
#      standard event is response.function_call_arguments.delta/.done, so the
#      SDK never accumulated the arguments.
#   3. reasoning items used summary:"" (string) — the SDK expects lists.
#   4. non-streaming responses lacked the required top-level
#      parallel_tool_calls / tool_choice / tools fields.
# Verified fixed by tests/e2e_runtime/sdk_codex_compat.py, which drives real
# servers and parses their output with the SDK; the tests below lock the
# shapes statically and at the unit level.


def test_codex_resp02_response_created_carries_full_response_object():
    """response.created must include object/created_at/output/usage so the
    SDK's stream snapshot has a real `output` list to append items to."""
    for wname in ('nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wname / 'src' / 'main.py').read_text()
        assert '"output": []' in src or "'output': []" in src, \
            f'{wname}: response.created missing output:[] (Codex snapshot crash)'
    nsrc = (ROOT / 'nvidia-python' / 'src' / 'responses_compat.py').read_text()
    assert "'output': output or []" in nsrc or '"output": output or []' in nsrc


def test_codex_resp02_no_nonstandard_function_call_event_names():
    """The Responses API standard event is response.function_call_arguments.delta
    / .done — response.function_call.delta is unknown to the openai SDK.
    AST-based: comments mentioning the old name are fine; emitted event-name
    string constants are not."""
    import ast
    BAD = 'response.function_call.delta'
    GOOD_DELTA = 'response.function_call_arguments.delta'
    GOOD_DONE = 'response.function_call_arguments.done'

    def emitted_strings(path):
        tree = ast.parse(path.read_text())
        return {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    for wname in ('nous', 'opencode', 'blackbox', 'openrouter'):
        consts = emitted_strings(ROOT / wname / 'src' / 'main.py')
        assert BAD not in consts, \
            f'{wname}: nonstandard response.function_call.delta event still emitted'
        assert GOOD_DELTA in consts, f'{wname}: missing standard function_call_arguments.delta'
        assert GOOD_DONE in consts, f'{wname}: missing standard function_call_arguments.done'
    nconsts = emitted_strings(ROOT / 'nvidia-python' / 'src' / 'responses_compat.py')
    assert BAD not in nconsts
    assert GOOD_DELTA in nconsts and GOOD_DONE in nconsts


def test_codex_resp02_reasoning_summary_is_a_list():
    """The SDK's ResponseReasoningItem.summary must be a list, never a string."""
    for wname in ('nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wname / 'src' / 'main.py').read_text()
        assert "'summary': ''" not in src and '"summary": ""' not in src, \
            f'{wname}: reasoning summary still an empty string'
        assert "'summary': []" in src or '"summary": []' in src, \
            f'{wname}: no list-typed reasoning summary'
    nsrc = (ROOT / 'nvidia-python' / 'src' / 'responses_compat.py').read_text()
    assert "'summary': ''" not in nsrc and "'summary': []," in nsrc


def test_codex_resp02_nonstreaming_response_has_sdk_required_fields():
    """client.responses.create() (non-streaming) requires top-level
    parallel_tool_calls / tool_choice / tools on the Response object."""
    for wname in ('nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wname / 'src' / 'main.py').read_text()
        assert 'parallel_tool_calls' in src, \
            f'{wname}: non-streaming response missing parallel_tool_calls'
        assert "'tool_choice': 'auto'" in src or '"tool_choice": "auto"' in src, \
            f'{wname}: non-streaming response missing tool_choice'
        assert "'tools': []" in src or '"tools": []' in src, \
            f'{wname}: non-streaming response missing tools'
    nsrc = (ROOT / 'nvidia-python' / 'src' / 'responses_compat.py').read_text()
    assert 'parallel_tool_calls' in nsrc and 'tool_choice' in nsrc and 'tools' in nsrc


def test_codex_resp02_sdk_parses_wrapper_stream_unit():
    """Unit-level SDK parse: the openai SDK stream parser must accept a stream
    shaped exactly like the wrappers now emit (full created + standard tool
    events + reasoning list summary), for text, tool-call and reasoning-only
    turns."""
    pytest.importorskip('openai')
    import httpx
    from openai import OpenAI

    def ev(e, **kw):
        return f'event: {e}\ndata: {json.dumps({"type": e, **kw})}\n\n'

    created = ev('response.created', response={'id': 'resp_1', 'object': 'response',
                'created_at': 1785600000, 'model': 'm', 'status': 'in_progress',
                'output': [], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}})
    inprog = ev('response.in_progress', response={'id': 'resp_1', 'status': 'in_progress'})
    msgadd = ev('response.output_item.added', output_index=0,
                item={'id': 'msg-1', 'type': 'message', 'status': 'in_progress',
                      'role': 'assistant', 'content': []})
    partadd = ev('response.content_part.added', item_id='msg-1', output_index=0,
                 content_index=0, part={'type': 'output_text', 'text': ''})
    fcadd = ev('response.output_item.added', output_index=1,
               item={'id': 'call_1', 'type': 'function_call', 'status': 'in_progress',
                     'call_id': 'call_1', 'name': 'lookup', 'arguments': ''})
    fcd1 = ev('response.function_call_arguments.delta', item_id='call_1',
              output_index=1, delta='{"x":')
    fcd2 = ev('response.function_call_arguments.delta', item_id='call_1',
              output_index=1, delta=':1}')
    fcdone = ev('response.function_call_arguments.done', item_id='call_1',
                output_index=1, name='lookup', arguments='{"x":1}')
    fcdone_item = ev('response.output_item.done', output_index=1,
                     item={'id': 'call_1', 'type': 'function_call', 'status': 'completed',
                           'call_id': 'call_1', 'name': 'lookup', 'arguments': '{"x":1}'})
    txtdone = ev('response.output_text.done', item_id='msg-1', output_index=0,
                 content_index=0, text='')
    partdone = ev('response.content_part.done', item_id='msg-1', output_index=0,
                  content_index=0, part={'type': 'output_text', 'text': '', 'annotations': []})
    msgdone = ev('response.output_item.done', output_index=0,
                 item={'id': 'msg-1', 'type': 'message', 'status': 'completed',
                       'role': 'assistant',
                       'content': [{'type': 'output_text', 'text': '', 'annotations': []}]})
    rsnadd = ev('response.output_item.added', output_index=1,
                item={'id': 'rsn_1', 'type': 'reasoning', 'status': 'in_progress',
                      'summary': [], 'content': []})
    rsnd = ev('response.reasoning_text.delta', item_id='rsn_1', output_index=1,
              content_index=0, delta='think')
    rsndone = ev('response.reasoning_text.done', item_id='rsn_1', output_index=1,
                 content_index=0, text='think')
    rsnitem = ev('response.output_item.done', output_index=1,
                 item={'id': 'rsn_1', 'type': 'reasoning', 'status': 'completed',
                       'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'think'}]})
    comp = ev('response.completed', response={'id': 'resp_1', 'object': 'response',
              'created_at': 1785600000, 'model': 'm', 'status': 'completed',
              'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [],
              'output': [], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}})

    def run(body, label):
        def make_transport(b):
            def handler(request):
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                      content=b.encode())
            return httpx.MockTransport(handler)
        client = OpenAI(api_key='sk-test', http_client=httpx.Client(transport=make_transport(body)))
        try:
            with client.responses.stream(model='m', input='hi') as stream:
                return list(stream)
        except Exception as e:  # pragma: no cover - assertion below reports
            raise AssertionError(f'{label}: openai SDK rejected stream: {type(e).__name__}: {e}') from e

    # tools turn
    evs = run(created + inprog + msgadd + partadd + fcadd + fcd1 + fcd2 + fcdone
              + fcdone_item + txtdone + partdone + msgdone + comp + 'data: [DONE]\n\n',
              'tools')
    names = [type(e).__name__ for e in evs]
    assert any('FunctionCallArgumentsDelta' in n for n in names), names
    assert any('FunctionCallArgumentsDone' in n for n in names), names
    # reasoning-only turn
    evs = run(created + inprog + msgadd + partadd + rsnadd + rsnd + rsndone
              + rsnitem + txtdone + partdone + msgdone + comp + 'data: [DONE]\n\n',
              'reasoning_only')
    names = [type(e).__name__ for e in evs]
    assert any('ReasoningTextDelta' in n for n in names), names
    assert any('ReasoningTextDone' in n for n in names), names


# ── Same-protocol passthrough ─────────────────────────────────────────────

def test_openai_chat_body_not_mutated_by_translators():
    """OpenAI→OpenAI must pass through: the translators must recognise an
    already-correct shape and leave it untouched (no double conversion)."""
    from common.translations import anthropic_to_openai_response, openai_to_anthropic_response
    oai = {"id": "chatcmpl-1", "object": "chat.completion", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}
    assert anthropic_to_openai_response(oai) == oai
    anthro = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert openai_to_anthropic_response(anthro) == anthro
    for wname in WRAPPERS:
        m = _load(wname)
        fn = _openai_to_anthropic_fn(m, wname)
        out = fn(dict(anthro))
        assert out == anthro or out.get('type') == 'message', \
            f'{wname}: Anthropic passthrough mutated already-Anthropic response: {out!r}'
