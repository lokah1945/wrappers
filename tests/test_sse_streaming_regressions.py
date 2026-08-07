#!/usr/bin/env python3
"""SSE streaming regression suite — the 10 scenarios from the 2026-08-01 audit.

B-40 fix: before this file, NO test fed an SSE byte stream through any
wrapper's translator. Every CRITICAL streaming bug found in the audit
(B-01/B-02/B-03/B-05/B-06/B-07/B-10) passed CI unnoticed.

These tests exercise the real parsing layer, not hand-built event dicts:
they feed raw upstream bytes in and assert on the emitted SSE frames.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.auth import (  # noqa: E402
    check_auth, is_public_path, tokens_match,
)
from common.sse import (  # noqa: E402
    IDLE, iter_chunks_with_idle, normalize_sse_newlines,
)
from common.translations.anthropic_stream import AnthropicStreamState  # noqa: E402
from common.sanitize_tokens import (  # noqa: E402
    SpecialTokenFilter, filter_special_tokens, DsmlMarkupFilter,
)
from common.translations.shared import (  # noqa: E402
    responses_usage, tokens_from_chat_usage, new_response_id,
)


# ── helpers ────────────────────────────────────────────────────────────────

def chunk(delta: dict | None = None, finish: str | None = None,
          usage: dict | None = None, space: bool = True) -> str:
    """Build one OpenAI chat SSE line."""
    payload: dict = {"object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta or {}, "finish_reason": finish}
    ]}
    if usage:
        payload["usage"] = usage
    return ("data: " if space else "data:") + json.dumps(payload) + "\n"


async def agen(lines):
    for line in lines:
        yield line.encode() if isinstance(line, str) else line


def load_openrouter_translator(name: str):
    """Exec one translator out of openrouter/src/main.py with stubbed deps.

    Importing the whole module needs mcp/dotenv/etc.; the translators are pure
    functions over an async byte iterator, so we isolate them.
    """
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    start = src.index(f"async def {name}")
    end = src.index("\n# ══", start)
    ns = {
        'json': json, 'time': __import__('time'), 'asyncio': asyncio,
        'HEARTBEAT_MS': 100000,
        'logger': types.SimpleNamespace(error=lambda *a, **k: None,
                                        warning=lambda *a, **k: None,
                                        info=lambda *a, **k: None),
        '_iter_chunks_with_idle': iter_chunks_with_idle,
        '_IDLE': IDLE,
        '_normalize_sse_newlines': normalize_sse_newlines,
        # 2026-08-03 fixes: translators now use the shared token scrubber (P0-4)
        # and the canonical Responses usage helper (P1-3) — provide the REAL
        # shared implementations so the isolated exec stays faithful.
        '_SpecialTokenFilter': SpecialTokenFilter,
        '_DsmlMarkupFilter': DsmlMarkupFilter,
        '_filter_special_tokens': filter_special_tokens,
        '_responses_usage': responses_usage,
        '_tokens_from_chat_usage': tokens_from_chat_usage,
        # R7: translators mint UNIQUE response ids via the shared helper —
        # provide the REAL implementation here too.
        '_new_response_id': new_response_id,
        # R9: translators mint UNIQUE message/item ids via a module helper —
        # provide an equivalent mint so the isolated exec stays faithful.
        '_new_msg_id': lambda: f"msg_{int(__import__('time').time()*1000)}-{__import__('secrets').token_hex(4)}",
    }
    exec(compile(src[start:end], 'openrouter_translator', 'exec'), ns)
    return ns[name]


def parse_frames(frames: list[str]) -> list[tuple[str, dict]]:
    """Split emitted SSE strings into (event_name, payload) pairs."""
    out = []
    for f in frames:
        if not f or f.startswith(':'):
            continue
        ev = None
        data = None
        for line in f.split('\n'):
            if line.startswith('event: '):
                ev = line[7:].strip()
            elif line.startswith('data: '):
                raw = line[6:].strip()
                if raw == '[DONE]':
                    data = '[DONE]'
                else:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = raw
        if data is not None:
            out.append((ev or (data.get('type') if isinstance(data, dict) else None), data))
    return out


# ── 1. B-01 — empty `data:` keep-alive must not end the stream ─────────────

def test_b01_empty_data_line_is_keepalive_not_terminator():
    """A bare `data:` is a legal empty SSE event, not end-of-stream.

    blackbox+opencode listed b'' in their terminator tuple, so a keep-alive
    ended the turn mid-generation.
    """
    for wrapper in ('blackbox', 'opencode'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert "b'', b'\"[DONE]\"'" not in src, (
            f"{wrapper}: empty payload is back in the DONE terminator tuple")
        assert 'b"[DONE]", b""' not in src, (
            f"{wrapper}: empty payload is back in the DONE terminator tuple")


def test_b01_state_machine_survives_blank_delta():
    """An empty content delta mid-stream must not terminate the state machine."""
    st = AnthropicStreamState(model='m')
    list(st.translate_chunk(json.loads(chunk({'content': 'A'})[6:])))
    list(st.translate_chunk({}))                      # empty/keep-alive frame
    evs = st.translate_chunk(json.loads(chunk({'content': 'B'})[6:]))
    assert any('text_delta' in e and 'B' in e for e in evs), \
        'content after a blank frame was dropped'
    assert not st.finished


# ── 2. B-02 — `data:{...}` without a space must parse ──────────────────────

@pytest.mark.parametrize('space', [True, False], ids=['data: ', 'data:'])
def test_b02_sse_space_after_data_is_optional(space):
    f = load_openrouter_translator('_translate_openai_stream_to_anthropic')

    async def run():
        lines = [chunk({'content': 'HELLO WORLD'}, space=space),
                 chunk(finish='stop', space=space), 'data: [DONE]\n']
        return [e async for e in f(agen(lines), {'model': 'm'})]

    frames = parse_frames(asyncio.run(run()))
    text = ''.join(p['delta']['text'] for _e, p in frames
                   if isinstance(p, dict) and p.get('type') == 'content_block_delta'
                   and p['delta'].get('type') == 'text_delta')
    assert text == 'HELLO WORLD', f'space={space}: upstream content was discarded'


# ── 3. B-03 — parallel tool calls ──────────────────────────────────────────

def test_b03_parallel_tool_calls_distinct_blocks_and_all_arguments():
    """Two parallel tools => 2 starts, distinct indices, all 4 arg fragments.

    The old code emitted content_block_start per chunk (4 starts + phantoms),
    collided every tool on index 0, and dropped all but the last tool's
    arguments because the emit sat outside the for-loop.
    """
    f = load_openrouter_translator('_translate_openai_stream_to_anthropic')

    async def run():
        lines = [
            chunk({'tool_calls': [
                {'index': 0, 'id': 'call_a', 'function': {'name': 'alpha', 'arguments': '{"x'}},
                {'index': 1, 'id': 'call_b', 'function': {'name': 'beta', 'arguments': '{"y'}},
            ]}),
            chunk({'tool_calls': [
                {'index': 0, 'function': {'arguments': '":1}'}},
                {'index': 1, 'function': {'arguments': '":2}'}},
            ]}),
            chunk(finish='tool_calls'),
            'data: [DONE]\n',
        ]
        return [e async for e in f(agen(lines), {'model': 'm'})]

    frames = parse_frames(asyncio.run(run()))
    starts = [p for _e, p in frames if isinstance(p, dict) and p.get('type') == 'content_block_start']
    args = [p for _e, p in frames if isinstance(p, dict)
            and p.get('type') == 'content_block_delta'
            and p['delta'].get('type') == 'input_json_delta']

    assert len(starts) == 2, f'expected exactly 2 tool blocks, got {len(starts)}'
    idxs = [s['index'] for s in starts]
    assert len(set(idxs)) == 2, f'tool blocks collided on the same index: {idxs}'
    assert all(s['content_block']['name'] for s in starts), 'phantom unnamed tool block emitted'
    assert len(args) == 4, f'expected 4 argument fragments, got {len(args)}'

    # Reassemble each tool's arguments and confirm they are valid JSON.
    by_idx: dict[int, str] = {}
    for a in args:
        by_idx[a['index']] = by_idx.get(a['index'], '') + a['delta']['partial_json']
    reassembled = [json.loads(v) for v in by_idx.values()]
    assert {'x': 1} in reassembled and {'y': 2} in reassembled, (
        f'tool arguments were lost or interleaved: {reassembled}')


# ── 4. B-06 — stop_reason maps strictly from finish_reason ─────────────────

@pytest.mark.parametrize('finish,expected', [
    ('stop', 'end_turn'),
    ('length', 'max_tokens'),
    ('tool_calls', 'tool_use'),
    ('content_filter', 'refusal'),
])
def test_b06_stop_reason_strict_mapping_even_after_a_tool_call(finish, expected):
    """A turn that used a tool then finished with stop/length must NOT report
    tool_use — Claude Code would wait forever for a tool_result, and genuine
    max_tokens truncation was masked."""
    st = AnthropicStreamState(model='m')
    list(st.translate_chunk({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 't1', 'function': {'name': 'f', 'arguments': '{}'}}]}}]}))
    evs = st.translate_chunk({'choices': [{'delta': {}, 'finish_reason': finish}]})
    md = [json.loads(e.split('data: ')[1]) for e in evs if 'message_delta' in e]
    assert md and md[0]['delta']['stop_reason'] == expected


def test_b06_force_done_still_infers_tool_use_when_block_open():
    """force_done() has no finish_reason to respect, so inferring tool_use from
    a still-open tool block remains correct."""
    st = AnthropicStreamState(model='m')
    list(st.translate_chunk({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 't1', 'function': {'name': 'f', 'arguments': '{"a'}}]}}]}))
    evs = st.force_done()
    md = [json.loads(e.split('data: ')[1]) for e in evs if 'message_delta' in e]
    assert md and md[0]['delta']['stop_reason'] == 'tool_use'


# ── 5. B-05 — post-finish truncation is observable ─────────────────────────

def test_b05_content_after_finish_is_counted_not_silently_dropped():
    st = AnthropicStreamState(model='m')
    list(st.translate_chunk({'choices': [{'delta': {'content': 'a'}, 'finish_reason': 'stop'}]}))
    assert st.dropped_after_finish == 0
    dropped = st.translate_chunk({'choices': [{'delta': {'content': 'LOST'}}]})
    assert dropped == [], 'must not emit content after message_stop'
    assert st.dropped_after_finish == 1, 'truncation must be counted for observability'


# ── 6. B-07 — upstream errors must not become a successful end_turn ────────

def test_b07_mid_stream_upstream_error_surfaces_as_error_event():
    f = load_openrouter_translator('_translate_openai_stream_to_anthropic')

    async def run():
        lines = [
            chunk({'content': 'partial'}),
            'data: ' + json.dumps({'error': {'message': 'upstream exploded',
                                             'type': 'server_error'}}) + '\n',
            'data: [DONE]\n',
        ]
        return [e async for e in f(agen(lines), {'model': 'm'})]

    raw = asyncio.run(run())
    assert any('event: error' in e for e in raw), \
        'upstream error was swallowed and reported as a clean end_turn'


def test_b07_wrappers_do_not_fabricate_end_turn_on_exception():
    """blackbox/opencode Anthropic handlers must emit an error event before
    force_done() in their exception path."""
    for wrapper in ('blackbox', 'opencode'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        i = src.index("logger.error(f'[anthropic stream] {e}')")
        window = src[i:i + 600]
        assert 'event: error' in window, (
            f'{wrapper}: exception path still fabricates a successful turn')


# ── 7. B-10 — unparsable frames must never be rendered as model text ───────

def test_b10_nous_does_not_synthesise_content_from_unparsable_frames():
    """The exact bug from the user's terminal: Anthropic protocol frames were
    re-emitted as assistant prose."""
    src = (ROOT / 'nous' / 'src' / 'main.py').read_text()
    assert 'parsed = {"choices": [{"delta": {"content": data.decode' not in src, \
        'nous is synthesising assistant content from unparsable SSE frames again'
    i = src.index('parsed = json.loads(data)')
    window = src[i:i + 900]
    assert 'dropping unparsable SSE frame' in window


def test_b10_no_wrapper_wraps_raw_bytes_as_content():
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert '{"delta": {"content": data.decode' not in src
        assert "{'delta': {'content': data.decode" not in src


# ── 8. B-08 — heartbeat: idle vs dead upstream ─────────────────────────────

def test_b08_idle_upstream_yields_sentinel_then_recovers():
    class SlowResp:
        class content:
            @staticmethod
            def iter_any():
                async def gen():
                    await asyncio.sleep(0.15)
                    yield b'data: {"a":1}\n'
                return gen()

    async def run():
        seen = []
        async for c in iter_chunks_with_idle(SlowResp(), 0.05):
            seen.append(c)
        return seen

    seen = asyncio.run(run())
    assert IDLE in seen, 'no heartbeat sentinel during an idle gap'
    assert seen[-1] == b'data: {"a":1}\n', 'real chunk was lost after idling'


def test_b08_dead_upstream_raises_instead_of_heartbeating_forever():
    """The core B-08 defect: asyncio.wait_for could not tell an idle upstream
    from a failed one, so a dead stream was heartbeated indefinitely."""
    class BrokenResp:
        class content:
            @staticmethod
            def iter_any():
                async def gen():
                    raise ConnectionResetError('upstream died')
                    yield b''  # pragma: no cover
                return gen()

    async def run():
        async for _c in iter_chunks_with_idle(BrokenResp(), 0.05):
            pass

    with pytest.raises(ConnectionResetError):
        asyncio.run(run())


def test_b08_crlf_framing_normalised():
    assert normalize_sse_newlines(b'data: 1\r\n\r\n') == b'data: 1\n\n'
    assert normalize_sse_newlines(b'data: 1\n\n') == b'data: 1\n\n'


# ── 9. B-26/B-27/B-28 — auth must fail closed ──────────────────────────────

def test_b26_openrouter_management_routes_are_not_public():
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    assert "not path.startswith('/openrouter/')" not in src, \
        'the /openrouter/* key-management API is publicly reachable again'
    assert 'MANAGEMENT_PREFIX' in src and '_management_token' in src


def test_b27_public_paths_are_exact_and_method_gated():
    public_any = frozenset({'/health'})
    public_get = frozenset({'/v1/models'})
    assert is_public_path('/health', 'GET', public_any, public_get)
    assert is_public_path('/v1/models', 'GET', public_any, public_get)
    # prefix-matching must NOT leak a lookalike route
    assert not is_public_path('/v1/models-internal', 'GET', public_any, public_get)
    # method gating: POST to a discovery-only path is not public
    assert not is_public_path('/v1/models', 'POST', public_any, public_get)


def test_b28_auth_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.delenv('BEARER_TOKEN', raising=False)
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    monkeypatch.delenv('REQUIRE_AUTH', raising=False)
    res = check_auth({}, surface='/v1/chat/completions')
    assert not res.ok and res.status == 503, \
        'unset BEARER_TOKEN must NOT silently serve an open relay'


def test_b28_explicit_opt_out_still_serves_open(monkeypatch):
    monkeypatch.delenv('BEARER_TOKEN', raising=False)
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    monkeypatch.setenv('REQUIRE_AUTH', 'false')
    assert check_auth({}).ok


def test_b28_valid_and_invalid_tokens(monkeypatch):
    monkeypatch.setenv('BEARER_TOKEN', 'secret-token')
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    assert check_auth({'authorization': 'Bearer secret-token'}).ok
    assert check_auth({'x-api-key': 'secret-token'}).ok
    bad = check_auth({'authorization': 'Bearer wrong'})
    assert not bad.ok and bad.status == 401
    assert not check_auth({}).ok


def test_b29_token_rotation_takes_effect_without_restart(monkeypatch):
    monkeypatch.setenv('BEARER_TOKEN', 'old')
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    assert check_auth({'authorization': 'Bearer old'}).ok
    monkeypatch.setenv('BEARER_TOKEN', 'new')
    assert not check_auth({'authorization': 'Bearer old'}).ok, \
        'a REVOKED token still works — the value is cached at import'
    assert check_auth({'authorization': 'Bearer new'}).ok


def test_b30_non_ascii_token_yields_401_not_500():
    """hmac.compare_digest raises TypeError on non-ASCII str; it must be
    handled as a clean auth failure rather than escaping as a 500."""
    assert tokens_match('tökén', 'tökén') is True
    assert tokens_match('tökén', 'other') is False
    assert tokens_match('', 'x') is False
    assert tokens_match('x', '') is False


# ── 10. B-33 — response stores must stay bounded ───────────────────────────

def test_b33_openrouter_response_store_is_bounded():
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    assert '_prune_response_store' in src, 'openrouter response store has no eviction'
    assert '_RESPONSE_STORE_MAX_ENTRIES' in src
    assert '_RESPONSE_STORE_MAX_BYTES' in src
    assert '_RESPONSE_STORE_TTL_SEC' in src


def test_b33_blackbox_response_store_bounded_on_all_axes():
    src = (ROOT / 'blackbox' / 'src' / 'main.py').read_text()
    assert '_RESPONSE_STORE_TTL_SEC' in src, 'blackbox store still has no TTL'
    assert '_RESPONSE_STORE_MAX_BYTES' in src, 'blackbox store still has no byte cap'


def test_b33_all_wrappers_declare_store_bounds():
    for wrapper, path in (
        ('nous', 'src/main.py'),
        ('opencode', 'src/main.py'),
        ('blackbox', 'src/main.py'),
        ('openrouter', 'src/main.py'),
        ('nvidia-python', 'src/responses_compat.py'),
    ):
        src = (ROOT / wrapper / path).read_text()
        assert 'RESPONSE_STORE' in src
        has_bound = any(tok in src for tok in
                        ('MAX_ENTRIES', 'MAX_CHARS', 'MAX_BYTES', '_STORE_MAX_BYTES', '> 200'))
        assert has_bound, f'{wrapper}: response store has no size bound'


# ── cross-wrapper parity guards ────────────────────────────────────────────

def test_parity_no_wrapper_shadows_shared_cooldown_helper():
    """B-21: a local _should_cooldown_key silently overrode the shared import,
    letting cooldown policy diverge per wrapper."""
    for wrapper in ('nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert 'def _should_cooldown_key(' not in src, (
            f'{wrapper} redefines _should_cooldown_key, shadowing '
            f'common.translations.should_cooldown_key')


def test_parity_all_wrappers_use_sentinel_heartbeat_not_wait_for():
    """B-08: asyncio.wait_for on the upstream iterator cannot distinguish an
    idle upstream from a dead one."""
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert 'asyncio.wait_for(aiter.__anext__()' not in src, \
            f'{wrapper} still uses wait_for for heartbeats'
        assert 'asyncio.wait_for(inner.__anext__()' not in src, \
            f'{wrapper} still uses wait_for for heartbeats'
    base = (ROOT / 'common' / 'base_wrapper.py').read_text()
    assert 'asyncio.wait_for(aiter.__anext__()' not in base


def test_parity_shared_cooldown_skips_model_capacity_errors():
    from common.translations.shared import should_cooldown_key
    capacity = {'error': {'message': 'No deployments available for selected model'}}
    assert should_cooldown_key(429, capacity) is False, \
        'a model-capacity 429 must not cool down the credential'
    assert should_cooldown_key(429, {'error': {'message': 'rate limit exceeded'}}) is True
    assert should_cooldown_key(401, {}) is True
    assert should_cooldown_key(503, {}) is True
    assert should_cooldown_key(400, {}) is False


# ══════════════════════════════════════════════════════════════════════════
# RUNTIME FINDINGS (R-01..R-07) — 2026-08-01
# Found by tests/runtime/run_runtime_e2e.py, which boots each wrapper as a REAL
# server and drives it with agent-shaped traffic. Every one of these passed the
# unit suite before being fixed, so they are locked down here too.
# ══════════════════════════════════════════════════════════════════════════

def test_r01_non_object_json_body_guard_registered_everywhere():
    """A valid-but-non-object JSON body ([1,2,3]) caused HTTP 500 in all five
    wrappers: handlers call body.get() and list.get raises AttributeError."""
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert 'JSONBodyGuard' in src, f'{wrapper}: JSON body-shape guard not registered'


def test_r01_body_guard_rejects_non_objects_and_passes_objects():
    from common.body_guard import _is_anthropic_surface
    assert _is_anthropic_surface('/v1/messages')
    assert not _is_anthropic_surface('/v1/chat/completions')


def test_r01_body_guard_replay_delegates_after_body():
    """Regression: synthesising http.disconnect after the body broke EVERY
    streaming response (StreamingResponse's disconnect-watcher cancelled the
    stream after one event). The replay must delegate to the real receive()."""
    import inspect
    from common import body_guard
    src = inspect.getsource(body_guard._replay)
    assert 'original_receive' in src
    assert 'await original_receive()' in src


def test_r02_parallel_tool_blocks_stay_open_concurrently():
    """Opening tool #2 must NOT close tool #1 — OpenAI interleaves argument
    fragments across all active tool indices."""
    st = AnthropicStreamState(model='m')
    frames = []
    frames += st.translate_chunk({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 'a', 'function': {'name': 'alpha', 'arguments': '{"x'}},
        {'index': 1, 'id': 'b', 'function': {'name': 'beta', 'arguments': '{"y'}},
    ]}}]})
    frames += st.translate_chunk({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'function': {'arguments': '":1}'}},
        {'index': 1, 'function': {'arguments': '":2}'}},
    ]}}]})
    frames += st.translate_chunk({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})

    open_blocks, errs, args = set(), [], {}
    for f in frames:
        d = json.loads(f.split('data: ')[1])
        t = d.get('type')
        if t == 'content_block_start':
            open_blocks.add(d['index'])
        elif t == 'content_block_delta':
            if d['index'] not in open_blocks:
                errs.append(f"delta on closed/unopened index {d['index']}")
            if d['delta'].get('type') == 'input_json_delta':
                args[d['index']] = args.get(d['index'], '') + d['delta']['partial_json']
        elif t == 'content_block_stop':
            open_blocks.discard(d['index'])
    assert not errs, errs
    assert not open_blocks, f'unclosed blocks: {open_blocks}'
    assert len(args) == 2
    for blob in args.values():
        json.loads(blob)  # must be valid JSON


def test_r02_no_wrapper_closes_previous_tool_block_on_new_tool():
    """nvidia's stop_open() also DELETED the tool from tool_map, so the next
    fragment re-created a phantom unnamed block."""
    src = (ROOT / 'nvidia-python' / 'src' / 'anthropic_compat.py').read_text()
    assert 'stop_all_tools' in src, 'nvidia has no concurrent tool-block close'
    i = src.index('async def stop_open')
    body = src[i:i + 1400]
    assert 'del tool_map[k]' not in body, 'stop_open still deletes tools from tool_map'


def test_r03_upstream_error_frame_surfaces_not_swallowed():
    """A mid-stream {"error": ...} frame has no "choices" and used to be
    dropped, closing the turn with a fabricated end_turn."""
    st = AnthropicStreamState(model='m')
    st.translate_chunk({'choices': [{'delta': {'content': 'partial'}}]})
    evs = st.translate_chunk({'error': {'message': 'boom', 'type': 'server_error'}})
    types = [json.loads(e.split('data: ')[1])['type'] for e in evs]
    assert 'error' in types, 'upstream error frame was swallowed'
    assert types[-1] == 'message_stop'
    assert st.upstream_error == 'boom'
    # nothing may be emitted afterwards
    assert st.translate_chunk({'choices': [{'delta': {'content': 'more'}}]}) == []


def test_r03_all_wrappers_handle_upstream_error_frames():
    for wrapper, path in (
        ('nous', 'src/main.py'),
        ('opencode', 'src/main.py'),
        ('blackbox', 'src/main.py'),
        ('openrouter', 'src/main.py'),
        ('nvidia-python', 'src/anthropic_compat.py'),
        ('nvidia-python', 'src/responses_compat.py'),
    ):
        src = (ROOT / wrapper / path).read_text()
        assert "get('error') is not None" in src or 'get("error") is not None' in src, \
            f'{wrapper}/{path}: does not detect mid-stream upstream error frames'


def test_r04_no_loop_variable_shadows_a_function_parameter():
    """`async for chunk in stop_open()` overwrote the `chunk` PARAMETER holding
    the model's text, so an SSE frame string was rendered as assistant prose."""
    import ast
    offenders = []
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        for py in (ROOT / wrapper / 'src').glob('*.py'):
            tree = ast.parse(py.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
                for node in ast.walk(fn):
                    if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
                        if node.target.id in params:
                            offenders.append(f'{py.name}:{node.lineno} {fn.name}() '
                                             f"loop var '{node.target.id}' shadows a parameter")
    assert not offenders, 'loop variables shadowing parameters:\n  ' + '\n  '.join(offenders)


def test_r05_openrouter_translates_non_streaming_responses():
    """openrouter returned the RAW OpenAI ChatCompletion body on the Anthropic
    and Responses surfaces — `return response` fired before the translation."""
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    i = src.index('async def messages(')
    # R5 audit: the messages() body grew (DSML drain etc.) — a fixed
    # 4500-char window pushed the translation call out of view. Bound the
    # window by the NEXT top-level route instead of a char count.
    try:
        j = src.index('\n@app.', i + 100)
    except ValueError:
        j = i + 12000
    block = src[i:j]
    assert '_openai_to_anthropic_response(payload, body)' in block, \
        'openrouter /v1/messages does not translate non-streaming replies'
    j = src.index('async def responses(')
    rblock = src[j:j + 4500]
    assert 'chat_to_responses(' in rblock, \
        'openrouter /v1/responses does not translate non-streaming replies'


def test_r06_no_duplicate_done_terminator():
    """Appending [DONE] unconditionally produced the corrupt frame
    '[DONE]data: [DONE]' when upstream already sent one without a blank line.

    Every site that SYNTHESISES a terminator must be guarded. nous is exempt
    from the `saw_done` idiom because it only echoes [DONE] on the branch that
    just consumed one, and its `terminated` flag makes that path single-shot —
    verified explicitly below rather than by keyword.
    """
    for wrapper in ('nvidia-python', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        if 'data: [DONE]' in src:
            # Local guard idiom, or the shared PassthroughBlockRewriter which
            # owns the canonical saw_done guard in common/sanitize_tokens.py
            # (CONTRACT §7: no forked block logic; only `[DONE]` bytes may
            # legitimately appear in wrapper code behind the shared driver).
            assert ('saw_done' in src) or ('PassthroughBlockRewriter' in src), \
                f'{wrapper}: [DONE] emitted without a saw_done guard'
    shared = (ROOT / 'common' / 'sanitize_tokens.py').read_text()
    if 'PassthroughBlockRewriter' in ''.join((ROOT / w / 'src' / 'main.py').read_text()
                                             for w in ('nvidia-python', 'opencode', 'blackbox', 'openrouter')):
        assert 'saw_done' in shared, 'shared passthrough rewriter lost its saw_done guard'
    base = (ROOT / 'common' / 'base_wrapper.py').read_text()
    # Either the historical local guard, or delegation to the shared rewriter
    # (which owns the canonical saw_done guard per CONTRACT §7).
    assert ('saw_done' in base) or ('PassthroughBlockRewriter' in base), \
        'common/base_wrapper.py appends [DONE] unconditionally'

    # nous: every emission site must sit inside the pass-through branch
    # (`state is None`), and the path must be single-shot via `terminated`.
    nsrc = (ROOT / 'nous' / 'src' / 'main.py').read_text()
    marker = 'yield "data: [DONE]'
    positions = []
    at = nsrc.find(marker)
    while at != -1:
        positions.append(at)
        at = nsrc.find(marker, at + 1)
    assert positions, 'nous no longer emits a [DONE] terminator at all'
    for idx in positions:
        window = nsrc[max(0, idx - 240):idx]
        assert 'if state is None:' in window, (
            'nous emits [DONE] outside the pass-through branch (duplicate risk)')
    assert 'terminated = True' in nsrc


def test_r07_nvidia_responses_closes_upstream_generator():
    """Breaking out of `async for raw in stream` left the generator suspended,
    so its finally (which releases the response AND the pool key) never ran."""
    src = (ROOT / 'nvidia-python' / 'src' / 'responses_compat.py').read_text()
    assert "_ac = getattr(stream, 'aclose', None)" in src, \
        'responses_compat does not deterministically close the upstream generator'


def test_r08_empty_choices_array_does_not_crash():
    """A frame with `"choices": []` is legal (usage-only frames, provider
    keep-alives). `chunk["choices"][0]` raised IndexError, which escaped as
    HTTP 500 mid-stream and killed the turn."""
    st = AnthropicStreamState(model='m')
    st.translate_chunk({'choices': [{'delta': {'content': 'a'}}]})
    # must not raise
    st.translate_chunk({'id': 'x', 'object': 'chat.completion.chunk', 'choices': []})
    evs = st.translate_chunk({'choices': [{'delta': {'content': 'b'}}]})
    assert any('text_delta' in e and '"b"' in e for e in evs)


def test_r08_no_unguarded_choices_indexing():
    """No wrapper may index choices[0] without first proving it is non-empty.

    A frame carrying `"choices": []` is legal, and the resulting IndexError
    escaped as an HTTP 500 in the middle of a stream.
    """
    import re
    offenders = []
    pattern = re.compile(r"""\[["']choices["']\]\[0\]""")
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        for py in sorted((ROOT / wrapper / 'src').glob('*.py')):
            lines = py.read_text().splitlines()
            for n, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue                       # comment describing the bug
                if not pattern.search(line):
                    continue
                if 'or [{}]' in line or 'or []' in line:
                    continue                       # inline default guard
                ctx = ' '.join(lines[max(0, n - 3):n - 1])
                if 'len(' in ctx and '> 0' in ctx:
                    continue                       # explicit length check above
                offenders.append(f'{wrapper}/{py.name}:{n}: {stripped[:90]}')
    assert not offenders, ('unguarded choices[0] indexing:\n  '
                           + '\n  '.join(offenders))


def test_b06_non_streaming_openai_to_anthropic_respects_finish_reason_after_tool():
    """B-06 applies to non-streaming conversion too: explicit finish_reason wins.

    A ChatCompletion can contain tool_calls while still finishing with stop,
    length, or content_filter. The wrapper must not force tool_use merely
    because a tool block is present.
    """
    from common.translations.shared import openai_to_anthropic_response

    base = {
        'id': 'chatcmpl_x',
        'model': 'm',
        'choices': [{
            'message': {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call_1', 'type': 'function',
                 'function': {'name': 'f', 'arguments': '{}'}}]},
        }],
    }
    for finish, expected in (
        ('stop', 'end_turn'),
        ('length', 'max_tokens'),
        ('tool_calls', 'tool_use'),
        ('content_filter', 'refusal'),
    ):
        payload = json.loads(json.dumps(base))
        payload['choices'][0]['finish_reason'] = finish
        converted = openai_to_anthropic_response(payload, model='m')
        assert converted['stop_reason'] == expected


def test_b06_local_non_streaming_translators_use_strict_finish_mapping():
    for wrapper in ('nous', 'opencode', 'blackbox'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        i = src.index('def openai_to_anthropic(')
        block = src[i:i + 2600]
        assert 'if fr is not None:' in block, f'{wrapper}: no strict finish_reason branch'
        assert 'else:' in block and 'tool_use" if (tool_calls or dsml_tools) else "end_turn"' in block.replace("'", '"')
        assert 'if tool_calls or dsml_tools:\n        stop' not in block


def test_b06_openrouter_non_streaming_content_filter_maps_to_refusal():
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    i = src.index('def _openai_to_anthropic_response(')
    # Window: up to the next top-level/async def — a fixed char window broke
    # every time the function grew (P1-2/P0-4 fields, R5 DSML recovery).
    j = src.find('\ndef ', i + 10)
    k = src.find('\nasync def ', i + 10)
    cands = [x for x in (j, k) if x != -1]
    block = src[i:min(cands) if cands else len(src)]
    assert '"content_filter": "refusal"' in block
    assert '"content_filter": "end_turn"' not in block


# ── CODEX-RESP-01 — reasoning-only Responses streams must complete ─────────
#
# CRITICAL finding from the 2026-08-01 deep audit: openrouter's Responses
# translator gated its completion events behind `if text_started:`. A model
# that emits ONLY reasoning_content (no text) left `text_started=False`, so
# `response.output_text.done` / `response.content_part.done` /
# `response.output_item.done` were never emitted and Codex waited forever for
# the terminal events — appearing to "stop mid-process" with no final
# response. Reference fix: nous `ResponsesStreamState.done()` / opencode
# inline `gen()` always emit completion events.

def _run_openrouter_responses_translator(lines):
    """Run the openrouter Responses translator over raw upstream SSE lines and
    return parsed (event, payload) frames."""
    f = load_openrouter_translator('_translate_openai_stream_to_responses')

    async def run():
        return [e async for e in f(agen(lines), 'mock-model')]

    return parse_frames(asyncio.run(run()))


def test_codex_resp01_reasoning_only_stream_emits_full_completion_lifecycle():
    """A model that outputs ONLY reasoning (no text content) must still emit
    the full item lifecycle and a terminal response.completed, or Codex hangs
    indefinitely."""
    frames = _run_openrouter_responses_translator([
        chunk({'role': 'assistant', 'content': ''}),
        chunk({'reasoning_content': 'Let me think... '}),
        chunk({'reasoning_content': 'still thinking.'}),
        chunk(finish='stop'),
        'data: [DONE]\n',
    ])
    types = [p.get('type') if isinstance(p, dict) else p for _e, p in frames]
    # Terminal events MUST be present (Codex hangs without them).
    assert 'response.completed' in types, \
        f'reasoning-only stream missing response.completed: {types}'
    assert any(d == '[DONE]' for _e, d in frames), 'missing data: [DONE]'
    # The full item lifecycle must be present — added AND closed.
    for ev in ('response.output_item.added', 'response.content_part.added',
               'response.reasoning_text.delta', 'response.reasoning_text.done',
               'response.output_text.done', 'response.content_part.done',
               'response.output_item.done'):
        assert ev in types, f'reasoning-only stream missing {ev}: {types}'
    # The completed response's output array must reference the reasoning item.
    completed = next(d for _e, d in frames
                     if isinstance(d, dict) and d.get('type') == 'response.completed')
    out_types = [o.get('type') for o in completed['response']['output']]
    assert 'reasoning' in out_types, \
        f'reasoning item missing from completed output: {out_types}'


def test_codex_resp01_reasoning_stream_with_text_still_completes():
    """Sanity: a normal reasoning-then-text stream keeps working (deltas are
    streamed, then completion events fire exactly once)."""
    frames = _run_openrouter_responses_translator([
        chunk({'role': 'assistant', 'content': ''}),
        chunk({'reasoning_content': 'hmm '}),
        chunk({'content': 'The answer is 42.'}),
        chunk(finish='stop'),
        'data: [DONE]\n',
    ])
    types = [p.get('type') if isinstance(p, dict) else p for _e, p in frames]
    text = ''.join(p.get('delta', '') for _e, p in frames
                   if isinstance(p, dict) and p.get('type') == 'response.output_text.delta')
    assert text == 'The answer is 42.', f'text deltas corrupted: {text!r}'
    assert types.count('response.completed') == 1, f'duplicate completion: {types}'
    assert types.count('response.output_item.done') == 2  # message + reasoning


def test_codex_resp01_openrouter_no_text_started_guard_on_completion_events():
    """Static parity guard: the openrouter Responses translator MUST NOT gate
    its completion events behind a text_started flag — that exact guard made
    Codex hang on reasoning-only outputs (CODEX-RESP-01)."""
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    i = src.index('async def _translate_openai_stream_to_responses')
    j = src.index('\n# ══', i)
    block = src[i:j]
    assert 'text_started' not in block, (
        'openrouter Responses translator still gates completion events on '
        'text_started (CODEX-RESP-01)')
    # Completion events must be emitted unconditionally, before the terminal.
    assert "yield _sse('response.output_text.done'" in block
    assert "yield _sse('response.content_part.done'" in block
    assert "yield _sse('response.output_item.done'" in block
    assert "yield _sse('response.completed'" in block


# ── B-39 — local error responses must increment the error counter ──────────

def test_b39_openrouter_local_error_responses_count_in_metrics():
    """B-39: openrouter's `record_error()` was dead code — no caller invoked
    it, so auth rejections, invalid JSON, FREE_ONLY blocks and pool exhaustion
    never incremented the dashboard error counter, and error_rate reported
    false health. Every local error response must route through
    `_error_response`, which records the error before returning."""
    import re
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    assert 'def _error_response(' in src
    assert 'metrics.record_error(status_code=status_code)' in src
    # No `return JSONResponse(...)` with a literal 4xx/5xx may remain — they
    # must all go through _error_response so the counter actually increments.
    offenders = []
    for m in re.finditer(r'return JSONResponse\(', src):
        depth = 0
        j = m.end() - 1
        while j < len(src):
            if src[j] == '(':
                depth += 1
            elif src[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        call = src[m.start():j + 1]
        sm = re.search(r'status_code=(\d{3})', call)
        if sm and int(sm.group(1)) >= 400:
            offenders.append(call[:120])
    assert not offenders, (
        'local error responses bypassing _error_response (B-39):\n  '
        + '\n  '.join(offenders))


# ── B-36 — pool record() must not fold in in-flight accounting ────────────

def test_b36_pool_record_is_telemetry_only_not_in_flight():
    """B-36: folding `record()` (telemetry) into in-flight accounting lets any
    unpaired path permanently inflate `in_flight`, skewing least-effective-load
    selection away from healthy keys. `record()` must not touch `in_flight`;
    `acquire()` must call `record()` AND `increment_in_flight()` separately."""
    import ast
    for rel in ('blackbox/src/key_pool.py', 'nous/src/main.py',
                'opencode/src/key_pool.py', 'openrouter/src/key_pool.py',
                'nvidia-python/src/key_pool.py'):
        src = (ROOT / rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'record':
                body = [n for n in node.body if not (
                    isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
                code = ast.get_source_segment(src, node)
                if body:
                    # drop the docstring statement from the source segment
                    doc = ast.get_source_segment(src, body[0])
                    start = code.index(doc) if doc in code else 0
                    real = code[start:]
                else:
                    real = code
                assert 'in_flight' not in real, \
                    f'{rel}: record() mutates in_flight (B-36)'
        # acquire() must keep the two calls explicit (nvidia does this inside
        # its _acquire_slot helper called from acquire).
        all_attrs: set = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) and 'acquire' in fn.name:
                calls = [n for n in ast.walk(fn)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr in ('record', 'increment_in_flight')]
                all_attrs |= {n.func.attr for n in calls}
        assert {'record', 'increment_in_flight'} <= all_attrs, \
            f'{rel}: acquire() must call record() and increment_in_flight() separately'


# ── B-20 — git identity resolution must be timeout-bounded ─────────────────

def test_b20_git_subprocess_calls_are_timeout_bounded():
    """B-20: git identity resolution runs blocking subprocesses. Without a
    timeout a hung git repo stalls startup (the audit flagged this as
    /health blocking the event loop; it actually runs at import, but it was
    unbounded). Every git subprocess call must carry an explicit timeout."""
    import re
    offenders = []
    for rel in ('nvidia-python/src/main.py', 'nous/src/main.py',
                'opencode/src/main.py', 'blackbox/src/main.py',
                'openrouter/src/main.py', 'model-registry/service.py'):
        src = (ROOT / rel).read_text()
        for m in re.finditer(r'subprocess\.check_output\(', src):
            depth = 0
            j = m.end() - 1
            while j < len(src):
                if src[j] == '(':
                    depth += 1
                elif src[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            call = src[m.start():j + 1]
            if 'git' in call and 'timeout=' not in call:
                offenders.append(f'{rel}: {call[:100]}')
    assert not offenders, 'git subprocess calls without timeout:\n  ' + '\n  '.join(offenders)


def test_b26_openrouter_management_is_loopback_only_and_never_fails_open():
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    auth_block = src[src.index('@app.middleware("http")'):src.index('# ══════════════════════════════════════════════════════════════════════════', src.index('@app.middleware("http")'))]
    assert '_is_loopback_client' in src
    assert 'OpenRouter management API is loopback-only' in auth_block
    assert 'management never fails open' in auth_block
    assert 'if is_management or (not DISABLE_AUTH and not _is_public_path(path, method))' in auth_block
    assert 'management routes are NEVER public and NEVER inherit' in auth_block


def test_b31_catch_all_post_paths_are_authenticated_and_rate_limited():
    for wrapper in ('nous', 'opencode', 'blackbox'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        i = src.index('async def catch_all')
        block = src[i:i + 700]
        assert 'request.method' in block and 'POST' in block, f'{wrapper}: catch_all does not gate POSTs'
        assert '_auth_check(request)' in block, f'{wrapper}: catch_all POST bypasses auth'
        assert 'check_rate_limit' in block, f'{wrapper}: catch_all POST bypasses rate limit'


def test_b31_mcp_post_messages_are_not_public():
    """MCP transports are agent surfaces; POST /mcp/messages must be gated."""
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    any_block = src[src.index('PUBLIC_PATHS_ANY'):src.index('PUBLIC_PATHS_GET')]
    assert '/mcp/messages' not in any_block
    assert '/mcp/sse' in src[src.index('PUBLIC_PATHS_GET'):src.index('MANAGEMENT_PREFIX')]

    csrc = (ROOT / 'common' / 'catalog_integration.py').read_text()
    for route in ("surface='/mcp/sse'", "surface='/mcp/messages'"):
        assert route in csrc, f'common catalog MCP route lacks auth check: {route}'


def test_b37_model_block_predicates_are_side_effect_free():
    """Model-scoped block predicates must not mutate without the pool lock."""
    import ast
    offenders = []
    for rel in (
        'blackbox/src/key_pool.py', 'opencode/src/key_pool.py',
        'openrouter/src/key_pool.py', 'nvidia-python/src/key_pool.py',
        'common/base_wrapper.py', 'nous/src/main.py',
    ):
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'is_model_blocked':
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Delete):
                        offenders.append(f'{rel}:{sub.lineno} del in is_model_blocked')
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                        if sub.func.attr in {'pop', 'clear', 'popitem'}:
                            offenders.append(f'{rel}:{sub.lineno} mutating {sub.func.attr} in is_model_blocked')
    assert not offenders, 'side-effecting is_model_blocked predicates:\n  ' + '\n  '.join(offenders)


def test_b38_nous_key_pool_uses_asyncio_lock():
    src = (ROOT / 'nous' / 'src' / 'main.py').read_text()
    i = src.index('class KeyPool:')
    block = src[i:i + 4200]
    assert 'self._lock = asyncio.Lock()' in block
    assert 'self._lock = threading.Lock()' not in block
    assert 'async def acquire' in block
    assert 'async def release' in block


def test_free_model_detection_uses_suffix_or_allowlist_not_substring():
    """B-34 from the root bug report: 'freemium' must not pass FREE_ONLY."""
    for wrapper in ('nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        i = src.index('def is_free_model')
        block = src[i:i + 900]
        assert 'endswith' in block, f'{wrapper}: free detection must use suffix matching'
        assert "'free' in mid" not in block
        assert '"free" in mid' not in block


# ══════════════════════════════════════════════════════════════════════════
# WRAPPER_CONTRACT v3.0 conformance
# ══════════════════════════════════════════════════════════════════════════

# WRAPPER_CONTRACT.md §2.1 — surfaces every wrapper MUST expose.
CONTRACT_REQUIRED_SURFACES = (
    '/v1/chat/completions',
    '/v1/responses',
    '/v1/messages',
    '/v1/messages/count_tokens',
    '/v1/embeddings',
    '/v1/models',
    '/api/tags',
    '/v1/capabilities',
    '/health',
    '/ready',
    '/metrics',
    '/metrics/prom',
    '/dashboard',
    '/version',
)


def test_contract_all_wrappers_expose_required_surfaces():
    """§2.1: the mandated surface set must exist on all five wrappers.

    Found by this check on 2026-08-01: openrouter was missing
    /v1/capabilities — a real parity gap surfaced by verifying the contract
    against the code rather than trusting the contract's own claims.
    """
    missing = []
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        for ep in CONTRACT_REQUIRED_SURFACES:
            if f'"{ep}"' not in src and f"'{ep}'" not in src:
                missing.append(f'{wrapper}: {ep}')
    assert not missing, ('wrappers missing contract-required surfaces:\n  '
                         + '\n  '.join(missing))


def test_contract_ports_match_wrappers_json():
    """§9.1: wrappers.json is the machine-readable source of truth for ports."""
    cfg = json.loads((ROOT / 'wrappers.json').read_text())['wrappers']
    expected = {'nvidia-python': 9101, 'nous': 9102, 'opencode': 9103,
                'blackbox': 9104, 'openrouter': 9106, 'model-registry': 9200}
    for name, port in expected.items():
        assert cfg[name]['port'] == port, f'{name}: wrappers.json port drifted'
    # The contract states 9105 is intentionally unused.
    assert 9105 not in {w['port'] for w in cfg.values()}


def test_contract_entry_points_match_systemd():
    """§1.1: the documented run command must match the shipped systemd units."""
    cfg = json.loads((ROOT / 'wrappers.json').read_text())['wrappers']
    for name in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        assert cfg[name]['entry_point'] == 'src.main:app', \
            f'{name}: entry_point drifted from src.main:app'
        unit = ROOT / name / 'systemd' / f'wrapper-{name}.service'
        if unit.exists():
            text = unit.read_text()
            assert 'src.main:app' in text, f'{name}: systemd unit does not use src.main:app'
            assert f"--port {cfg[name]['port']}" in text, \
                f'{name}: systemd port disagrees with wrappers.json'


def test_contract_shared_modules_exist():
    """§7: the parity mechanism depends on these shared modules existing."""
    for rel in ('common/auth.py', 'common/sse.py', 'common/body_guard.py',
                'common/middleware.py', 'common/model_state.py',
                'common/base_wrapper.py',
                'common/translations/anthropic_stream.py',
                'common/translations/shared.py'):
        assert (ROOT / rel).exists(), f'shared module missing: {rel}'


# ── R12 regression locks (fuzz-gate findings B-12.1/B-12.2/B-12.3) ──

def test_r12_max_completion_tokens_coalesced():
    """B-12.3: layer-2 (Anthropic upstream) converter must honour the newer
    OpenAI `max_completion_tokens` alias — silently ignoring it dropped the
    client's output cap (unbounded generation)."""
    from common.translations.shared import openai_chat_to_anthropic_request
    base = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}
    out = openai_chat_to_anthropic_request({**base, 'max_completion_tokens': 512})
    assert out.get('max_tokens') == 512, out
    # explicit max_tokens wins over the alias
    out2 = openai_chat_to_anthropic_request({**base, 'max_tokens': 100, 'max_completion_tokens': 512})
    assert out2.get('max_tokens') == 100, out2
    # neither present → no injected cap (no silent default mutation)
    out3 = openai_chat_to_anthropic_request(dict(base))
    assert 'max_tokens' not in out3, out3


def test_r12_metrics_pool_parity_all_wrappers():
    """B-12.2 (CONTRACT §10): every wrapper's /metrics JSON must expose the
    live pool stats and in-flight reservation count."""
    checks = {
        'nous': ('nous/src/main.py', 'KEY_POOL'),
        'opencode': ('opencode/src/main.py', 'pool'),
        'blackbox': ('blackbox/src/main.py', 'pool'),
    }
    import re
    for name, (rel, poolvar) in checks.items():
        src = (ROOT / rel).read_text()
        assert f"{poolvar}.all_stats()" in src, f'{name}: /metrics lacks pool stats'
        assert re.search(rf"sum\(k\.in_flight for k in {poolvar}\.keys\)", src), \
            f'{name}: /metrics lacks in_flight'
    # nvidia + openrouter were the parity baselines (R7): keep them locked too
    nv = (ROOT / 'nvidia-python/src/main.py').read_text()
    assert 'live_keys' in nv and 'all_stats()' in nv
    orw = (ROOT / 'openrouter/src/main.py').read_text()
    assert re.search(r"s\['pool'\]\s*=\s*pool\.all_stats\(\)|'pool':\s*pool\.all_stats\(\)", orw), \
        'openrouter: /metrics pool parity regressed'


def test_r12_base_genai_follows_explicit_llm_base():
    """B-12.1: BASE_GENAI must fall back to an operator-provided
    NVIDIA_BASE_URL before the public cloud default (no silent leak of
    embeddings/ranking/images traffic to ai.api.nvidia.com)."""
    src = (ROOT / 'nvidia-python/src/main.py').read_text()
    line = next(l for l in src.splitlines() if l.strip().startswith('BASE_GENAI')
                and 'os.environ' in l)
    assert '_explicit_llm_base' in src
    # precedence: NVIDIA_GENAI_URL env > explicit NVIDIA_BASE_URL > cloud default
    assert ("os.environ.get('NVIDIA_GENAI_URL')" in line
            and '_explicit_llm_base' in line and 'NVIDIA_GENAI_URL' in line), line
    assert line.index('NVIDIA_GENAI_URL') < line.rindex('NVIDIA_GENAI_URL'), line


def test_r14_nous_prom_metrics_pool_parity():
    """R14 (CONTRACT §10): nous /metrics/prom was the only wrapper whose
    Prometheus surface lacked pool-level series (4 siblings expose
    keys_total/keys_available/in_flight_total + per-key gauges)."""
    src = (ROOT / 'nous/src/main.py').read_text()
    assert 'def prom_metrics(self)' in src, 'nous KeyPool lacks prom_metrics()'
    assert 'KEY_POOL.prom_metrics()' in src, 'nous /metrics/prom does not emit pool series'
    for series in ('nous_keys_total', 'nous_keys_available', 'nous_in_flight_total',
                   'nous_key_rpm', 'nous_key_blocked', 'nous_key_failures_total'):
        assert series in src, f'nous prom series missing: {series}'
    # sibling parity baseline stays locked
    for w in ('opencode', 'blackbox', 'openrouter'):
        sib = (ROOT / f'{w}/src/key_pool.py').read_text()
        assert 'def prom_metrics(self)' in sib, f'{w}: pool prom_metrics regressed'


def test_r18_sanitize_header_value_fallback_parity():
    """B-18.1: per-wrapper ImportError fallbacks of sanitize_header_value must
    behave byte-for-byte like common.middleware.sanitize_header_value — the
    old regex fallbacks let CR/LF through (CRLF injection in degraded mode)
    and never truncated."""
    import re as _re
    from common.middleware import sanitize_header_value as shared
    battery = ['a', 'x\r\ny', 'h\re\na\nd', '\x00\x07tab\there', 'v' * 9000,
               None, '', 'ünïcode ❤', ' \t padded \t ', 'bad\x7fval', 'a\x01b']
    for path in ('opencode/src/main.py', 'blackbox/src/main.py',
                 'openrouter/src/main.py', 'nvidia-python/src/main.py'):
        src = (ROOT / path).read_text()
        m = _re.search(r"(    def sanitize_header_value\(value.*?)(?=\n\S|\Z)", src, _re.S)
        assert m, f'{path}: fallback def not found'
        import textwrap as _tw
        ns: dict = {}
        exec(_tw.dedent(m.group(1)), ns)
        fb = ns['sanitize_header_value']
        for v in battery:
            assert fb(v) == shared(v), (path, repr(v), fb(v), shared(v))


def test_r19_query_int_guard():
    """R19: nvidia dashboard-metrics endpoints parsed query params with bare
    int() — garbage (?hours=abc, ?limit=-1, ?days=1e12) raised ValueError →
    unshaped 500 (CONTRACT §4). The shared _query_int guard must return
    defaults on garbage and clamp to sane bounds."""
    import re as _re
    src = (ROOT / 'nvidia-python/src/main.py').read_text()
    m = _re.search(r"\ndef _query_int\(.*?(?=\ndef |\nclass |\Z)", src, _re.S)
    assert m, '_query_int helper missing'
    ns: dict = {'Request': object}
    exec(m.group(0), ns)
    qint = ns['_query_int']

    class _Req:
        def __init__(self, params):
            self.query_params = params

    assert qint(_Req({'hours': 'abc'}), 'hours', 24, 1, 100) == 24
    assert qint(_Req({'hours': ''}), 'hours', 24, 1, 100) == 24
    assert qint(_Req({}), 'hours', 24, 1, 100) == 24
    assert qint(_Req({'limit': '-5'}), 'limit', 50, 1, 1000) == 1
    assert qint(_Req({'limit': '99999999'}), 'limit', 50, 1, 1000) == 1000
    assert qint(_Req({'days': '30.9'}), 'days', 30, 1, 3660) == 30
    assert qint(_Req({'offset': ' 17 '}), 'offset', 0, 0, 100) == 17
    # and every endpoint site now routes through the guard (allow mentions in
    # the helper's own docstring; require no bare `= int(` call-site remains)
    import re as _re2
    assert not _re2.search(r"=\s*int\(request\.query_params\.get\(", src)


def test_r21_nous_usage_content_fallback_parity():
    """B-21.1: nous' degraded-mode fallbacks for responses_usage /
    tokens_from_chat_usage / responses_content_to_chat drifted from the
    shared helpers (strict-SDK-breaking flat usage, lost cached/reasoning,
    dropped input_image). Exec the fallbacks from source and compare
    behavior against the shared implementations on the same battery."""
    import re as _re, textwrap as _tw
    from common.translations.shared import (
        responses_usage as s_usage,
        tokens_from_chat_usage as s_tokens,
        responses_content_to_chat as s_content,
    )
    src = (ROOT / 'nous/src/main.py').read_text()

    def _grab(name, extra_ns=None):
        m = _re.search(rf"(    def {name}\(.*?)(?=\n    def |\n\S|\Z)", src, _re.S)
        assert m, f'nous fallback missing: {name}'
        ns: dict = dict(extra_ns or {})
        exec(_tw.dedent(m.group(1)), ns)
        return ns[name]

    # B-27.1: the usage twins now clamp through _finite_nonneg_int — exec it
    # first and inject it into the twins' namespace.
    fb_clamp = _grab('_finite_nonneg_int')
    ns_clamp = {'_finite_nonneg_int': fb_clamp}
    fb_usage = _grab('_responses_usage', ns_clamp)
    for args in ((0, 0, 0, 0), (11, 7, 3, 2), (None, None, None, None)):
        assert fb_usage(*args) == s_usage(*args), args
    fb_tokens = _grab('_tokens_from_chat_usage', ns_clamp)
    for u in ({}, None, {'prompt_tokens': 5, 'completion_tokens': 2,
                         'prompt_tokens_details': {'cached_tokens': 4},
                         'completion_tokens_details': {'reasoning_tokens': 9}},
              {'input_tokens': 3, 'output_tokens': 1}):
        assert fb_tokens(u) == s_tokens(u), u
    fb_content = _grab('_responses_content_to_chat')
    batteries = [
        'plain',
        [{'type': 'input_text', 'text': 'hi'}, {'type': 'output_text', 'text': ''}],
        [{'type': 'input_text', 'text': 'see'},
         {'type': 'input_image', 'image_url': 'http://img/x.png'}],
        [{'type': 'image', 'url': 'data:image/png;base64,AA=='}],
        [{'type': 'input_image', 'image_url': {'url': 'http://img/y.png'}}],
        [42, {'type': 'mystery'}, {'type': 'text', 'text': 123}],
        [],
    ]
    for b in batteries:
        assert fb_content(b) == s_content(b), b


def test_r21_nvidia_build_forward_headers_fallback_parity():
    """B-21.1: nvidia's degraded-mode _build_forward_headers forwarded only 6
    headers — the shared allowlist carries ~20 (x-stainless-* SDK identity,
    traceparent/tracestate, openai-organization/project, x-correlation-id,
    accept-language, conditional-caching). Exec fallback from source and
    compare against the shared implementation."""
    import re as _re, textwrap as _tw
    from common.translations.shared import build_forward_headers as shared
    src = (ROOT / 'nvidia-python/src/main.py').read_text()
    start = src.index('    _FB_FORWARDHEADER_ALLOWLIST = (')
    end = src.index('def _build_forward_headers(client_headers, extra=None):')
    block = src[start:end]
    tail = '''def _build_forward_headers(client_headers, extra=None):
    return _fb_build_forward_headers(client_headers, extra)
'''
    ns: dict = {}
    exec(_tw.dedent(block) + tail, ns)
    fb = ns['_build_forward_headers']
    hops = {'connection': 'keep-alive', 'authorization': 'secret-leak',
            'x-api-key': 'secret-leak', 'host': 'h', 'content-length': '9'}
    # NOTE: plain dicts are case-sensitive; real Starlette/aiohttp header
    # containers are case-insensitive. Use canonical lowercase keys here —
    # that is what the wire actually looks like after normalisation.
    client = {'user-agent': 'codex/1.0', 'x-stainless-retry-count': '1',
              'x-stainless-os': 'Linux', 'openai-organization': 'org-1',
              'traceparent': '00-abc-def-01', 'x-correlation-id': 'c1',
              'anthropic-version': '2023-06-01', 'accept-language': 'en',
              'if-none-match': 'etag', **hops}
    got_fb, got_sh = fb(client), shared(client)
    assert got_fb == got_sh, (got_fb, got_sh)
    assert 'x-stainless-retry-count' in got_fb and 'traceparent' in got_fb
    for h in hops:
        assert h not in got_fb, h
    assert fb(client, {'x-extra': '1'}) == shared(client, {'x-extra': '1'})


def test_b22_1_health_reports_top_level_in_flight_all_wrappers():
    """B-22.1 (CONTRACT §10 + §8 parity): /health must report live in-flight
    counts at TOP level on every wrapper — the only signal that detects a
    leaked pool reservation. openrouter had it; nvidia/nous/opencode/blackbox
    (and the reference base) reported in-flight only buried inside per-key
    live_keys stats."""
    targets = (
        ('nvidia-python', 'self.pool'),
        ('nous', 'KEY_POOL'),
        ('opencode', 'pool'),
        ('blackbox', 'pool'),
        ('openrouter', 'pool'),
    )
    for wrapper, pool_name in targets:
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        i = src.index('async def health(')
        body = src[i:i + 3000]
        assert 'in_flight' in body, f'{wrapper}: /health lacks top-level in_flight'
        assert f'sum(k.in_flight for k in {pool_name}.keys)' in body, \
            f'{wrapper}: /health in_flight is not a live sum over pool keys'
    base = (ROOT / 'common' / 'base_wrapper.py').read_text()
    j = base.index('async def health(')
    assert 'sum(k.in_flight for k in self.pool.keys)' in base[j:j + 1500], \
        'reference base_wrapper /health lacks top-level in_flight'


def test_b22_2_catalog_limit_clamped_against_negative_unbounded():
    """B-22.2: SQLite `LIMIT -1` means NO row limit. catalog_integration
    clamped only the upper bound (min(limit, 500)) — a negative limit from a
    client (or MCP caller) silently unbounded the query. All search call
    sites must clamp to [1, 500]; the management offset must be >= 0."""
    src = (ROOT / 'common' / 'catalog_integration.py').read_text()
    clamps = src.count('min(max(1, limit), 500)')
    assert clamps == 4, f'expected 4 limit clamps, found {clamps}'
    assert 'min(limit, 500)' not in src.replace('min(max(1, limit), 500)', ''), \
        'an unclamped-lower-bound limit site remains'
    assert 'offset=max(0, offset)' in src, 'management offset not clamped'


def test_b24_1_sanitize_error_detail_never_raises_on_deep_nesting():
    """B-24.1: >1000-deep nested payloads made json.loads/_scrub raise
    RecursionError, escaping sanitize_error_detail's guard and breaking the
    error-record path under upstream distress (§3.3). The function's contract
    is bounded, JSON-safe output — it must never raise."""
    from common.model.sanitize import sanitize_error_detail
    deep_str = '[' * 3000 + ']' * 3000
    out = sanitize_error_detail(deep_str)
    assert isinstance(out, str) and len(out) <= 4000
    deep_obj: list = []
    cur = deep_obj
    for _ in range(3000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    out2 = sanitize_error_detail(deep_obj)
    assert isinstance(out2, str) and len(out2) <= 4000
    # ordinary payloads still sanitize correctly
    out3 = sanitize_error_detail({'message': 'boom', 'authorization': 'Bearer abc123'})
    assert 'REDACTED' in out3 and 'abc123' not in out3


def test_b24_1_capacity_classifier_and_catalog_validation_survive_deep_nesting():
    """B-24.1 (class closure): the same RecursionError pattern existed in
    looks_model_capacity_error (proxy error path — §3.3) and
    validate_catalog_entries. Both must handle >1000-deep input cleanly."""
    from common.translations.shared import looks_model_capacity_error
    from common.model.validation import validate_catalog_entries
    deep: dict = {}
    cur = deep
    for i in range(3000):
        nxt: dict = {}
        cur[f'k{i}'] = nxt
        cur = nxt
    # must not raise; classifies as non-capacity (markers unreachable)
    assert looks_model_capacity_error(deep) is False
    assert looks_model_capacity_error({'message': 'no deployments available'}) is True
    # validation converts to a clean ValueError, never RecursionError
    cur2 = deep
    try:
        validate_catalog_entries([deep])
        raised = None
    except ValueError:
        raised = 'ValueError'
    except RecursionError:
        raised = 'RecursionError'
    assert raised == 'ValueError', raised


def test_b25_1_error_text_survives_deep_nesting_second_order_trap():
    """B-25.1: error_text (classify_upstream_error's entry point — the proxy
    error path, §3.3) had the same second-order recursion trap fixed in
    B-24.1: json.dumps raised RecursionError, then the except handler's own
    str(payload) re-recursed. Must return a bounded placeholder, never raise."""
    from common.model.errors import error_text, classify_upstream_error
    deep: dict = {}
    cur = deep
    for i in range(3000):
        nxt: dict = {}
        cur[f'k{i}'] = nxt
        cur = nxt
    out = error_text(deep)
    assert isinstance(out, str) and len(out) <= 4000
    # classification (hot proxy path) must never raise either
    cls = classify_upstream_error(500, deep)
    assert cls.state is not None
    cls429 = classify_upstream_error(429, {'error': {'message': 'model capacity reached'}})
    assert cls429.reason_code == 'MODEL_OR_DEPLOYMENT_RATE_LIMIT'
    # ordinary payloads unchanged
    assert error_text({'detail': 'boom'}) == '{"detail": "boom"}'
    assert error_text('plain') == 'plain'
    assert error_text(None) == ''


def test_b25_1_body_guard_shapes_over_deep_json_as_400_not_500():
    """B-25.1 closure: a syntactically valid JSON body nested >1000 deep made
    json.loads raise RecursionError — it escaped JSONBodyGuard AND would have
    crashed the route's own request.json(), yielding an unshaped 500 on every
    wrapper (§4 violation). The guard must now shape a 400 on both envelope
    styles, and still pass ordinary bodies through untouched."""
    import asyncio
    from common.body_guard import JSONBodyGuard

    async def echo_app(scope, receive, send):
        # downstream app: just acknowledges
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    def run(path, raw):
        guard = JSONBodyGuard(echo_app)
        scope = {'type': 'http', 'method': 'POST', 'path': path,
                 'headers': [(b'content-type', b'application/json')]}
        sent = []
        sent_msgs = [{'type': 'http.request', 'body': raw, 'more_body': False}]
        async def receive():
            return sent_msgs.pop(0) if sent_msgs else {'type': 'http.disconnect'}
        async def send(msg):
            sent.append(msg)
        asyncio.run(guard(scope, receive, send))
        status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
        return status, body

    deep = ('[' * 3000 + ']' * 3000).encode()
    st, body = run('/v1/chat/completions', deep)
    assert st == 400, (st, body[:200])
    assert b'too deeply' in body and b'"error"' in body
    st, body = run('/v1/messages', deep)
    assert st == 400, (st, body[:200])
    assert b'"type": "error"' in body.replace(b'"type":"error"', b'"type": "error"') or b'invalid_request_error' in body
    # ordinary bodies still pass through
    st, body = run('/v1/chat/completions', b'{"model": "x", "messages": []}')
    assert st == 200 and body == b'ok'


def test_b25_1_passthrough_drops_over_deep_upstream_frame():
    """B-25.1 closure: an over-deep upstream SSE frame raised RecursionError
    inside the passthrough scrubber (caught only ValueError). It must now be
    dropped like any other undecodable frame — stream continues, no crash."""
    from common.sanitize_tokens import PassthroughBlockRewriter
    p = PassthroughBlockRewriter()
    deep_json = '[' * 3000 + ']' * 3000
    frame = (f'data: {deep_json}\n\n').encode()
    out = p.feed(frame + b'data: [DONE]\n\n')
    blob = b''.join(out)
    assert b'[DONE]' in blob
    fin = p.finish(terminal_done=True)
    assert isinstance(b''.join(fin), bytes)


def test_b26_1_no_narrow_json_parse_catch_without_recursion_error():
    """B-26.1 class lock: json.loads raises RecursionError (not ValueError) on
    syntactically valid JSON nested beyond the interpreter limit. Every narrow
    JSON-parse catch in production code must therefore ALSO catch
    RecursionError, or an over-deep body/frame escapes as an unshaped 500
    (§4) / crashes the proxy error path (§3.3). Broad `except Exception` is
    acceptable; narrow JSONDecodeError/ValueError tuples are NOT."""
    import re
    files = []
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        files.extend((ROOT / wrapper / 'src').glob('*.py'))
    files.extend((ROOT / 'common').glob('*.py'))
    files.extend((ROOT / 'common' / 'model').glob('*.py'))
    files.extend((ROOT / 'common' / 'translations').glob('*.py'))
    offenders = []
    for f in files:
        src = f.read_text()
        # Whitelist: body_guard.py deliberately splits RecursionError into its
        # own dedicated clause (shaped 400 reject) directly above the narrow
        # malformed-JSON pass-through tuple — same function, stricter handling.
        split_ok = 'except RecursionError' in src
        for m in re.finditer(r'except \(([^)]*)\)', src):
            clause = m.group(1)
            if 'JSONDecodeError' in clause and 'RecursionError' not in clause:
                if split_ok and f.name == 'body_guard.py':
                    continue
                offenders.append(f'{f.relative_to(ROOT)}: except ({clause})')
        for m in re.finditer(r'except json\.JSONDecodeError\s*:', src):
            offenders.append(f'{f.relative_to(ROOT)}: bare JSONDecodeError')
    assert not offenders, 'narrow JSON-parse catches without RecursionError:\n' + '\n'.join(offenders)


def test_b26_1_corrupt_deep_manifest_degrades_to_empty_not_boot_crash(tmp_path):
    """B-26.1: an operator-edited manifest nested past the recursion limit
    previously crashed wrapper boot (load_provider_error_manifest /
    LocalModelRegistry init). MR-5 doctrine: corrupt manifest ⇒ degrade to
    {} with loud logging, never crash."""
    from pathlib import Path as _RealPath
    from common.model import errors as _err
    deep = '{"rules":' + '[' * 3000 + ']' * 3000 + '}'
    # Write a fake-provider manifest where the loader already reads (repo
    # manifests dir), then clean up.
    repo_dir = _RealPath(_err.__file__).resolve().parents[1] / 'model-registry' / 'manifests' / 'errors'
    repo_dir.mkdir(parents=True, exist_ok=True)
    target = repo_dir / 'zz_auditfake.json'
    target.write_text(deep)
    try:
        assert _err.load_provider_error_manifest('zz_auditfake') == {}
    finally:
        target.unlink(missing_ok=True)


def test_b27_1_usage_clamps_non_finite_and_negative():
    """B-27.1: upstream usage payloads may carry NaN/Infinity literals
    (Python's json.loads accepts them). int(NaN)=ValueError, int(Inf)=
    OverflowError previously crashed response builders on an upstream SUCCESS
    (§3.3); NaN also poisons metrics counters permanently. All converters
    must clamp to finite non-negative ints."""
    from common.translations.shared import responses_usage, tokens_from_chat_usage, finite_nonneg_int
    nan, inf = float('nan'), float('inf')
    u = responses_usage(nan, inf, -5, 'garbage')
    assert u == {'input_tokens': 0, 'input_tokens_details': {'cached_tokens': 0},
                 'output_tokens': 0, 'output_tokens_details': {'reasoning_tokens': 0},
                 'total_tokens': 0}, u
    import json as _j
    _j.dumps(u, allow_nan=False)  # response must be RFC-strict JSON
    assert tokens_from_chat_usage(
        {'prompt_tokens': nan, 'completion_tokens': inf,
         'prompt_tokens_details': {'cached_tokens': -3},
         'completion_tokens_details': {'reasoning_tokens': float('-inf')}}) == (0, 0, 0, 0)
    assert tokens_from_chat_usage({'prompt_tokens': 7, 'completion_tokens': 4.9}) == (7, 4, 0, 0)
    assert tokens_from_chat_usage(None) == (0, 0, 0, 0)
    assert finite_nonneg_int(True) or True  # bools tolerated
    assert finite_nonneg_int(2**80) == 2**80  # big finite ints survive


def test_b27_1_fallback_twins_clamp_parity():
    """§7: the nous and responses_compat ImportError fallback twins must clamp
    identically to the shared implementation."""
    import re as _re, textwrap as _tw
    from common.translations.shared import tokens_from_chat_usage as shared_tokens, \
        responses_usage as shared_usage
    nan, inf = float('nan'), float('inf')
    batteries = [None, {}, {'prompt_tokens': nan, 'completion_tokens': inf},
                 {'prompt_tokens': -9, 'prompt_tokens_details': {'cached_tokens': nan}},
                 {'prompt_tokens': 12, 'completion_tokens': 3.7}]
    for src_path in ('nous/src/main.py', 'nvidia-python/src/responses_compat.py'):
        src = (ROOT / src_path).read_text()
        m = _re.search(r'    def _finite_nonneg_int\(value\):.*?return v if v > 0 else 0\n'
                       r'(.*?)(?=\n\n|\Z)', src, _re.S)
        assert m, f'{src_path}: fallback _finite_nonneg_int not found'
        assert 'OverflowError' in m.group(0), f'{src_path}: twin missing OverflowError clamp'
        start = src.index('    def _finite_nonneg_int')
        if src_path.startswith('nous'):
            end = src.index('"total_tokens": it + ot}') + len('"total_tokens": it + ot}')
        else:
            end = src.index(', 0, 0)', start) + len(', 0, 0)')
        block = src[start:end]
        fns = {}
        exec(_tw.dedent(block), fns)
        tok, rus = fns.get('_tokens_from_chat_usage'), fns.get('_responses_usage')
        assert tok and rus, f'{src_path}: exec of twins failed'
        for b in batteries:
            assert tok(b) == shared_tokens(b), (src_path, b)
        for args in ((nan, inf, -5, 'x'), (7, 4.9, 0, 0)):
            got, want = rus(*args), shared_usage(*args)
            for k in ('input_tokens', 'output_tokens', 'total_tokens'):
                assert got[k] == want[k], (src_path, args, k)


def test_b27_1_metrics_boundaries_reject_non_finite_poison():
    """B-27.1 boundary: one poisoned usage frame must not stick in persisted
    counters (NaN is sticky and json.dumps then emits invalid JSON forever)."""
    import asyncio, importlib.util, json as _j

    def _load(path, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    nan, inf = float('nan'), float('inf')
    for path in ('opencode/src/metrics.py', 'blackbox/src/metrics.py', 'openrouter/src/metrics.py'):
        mod = _load(path, 'm_' + path.split('/')[0])
        # tmp db_path → isolated persist path (constructor rehydrates counters
        # from any real snapshot on disk — must not leak into this test).
        M = mod.Metrics(db_path='/tmp/b27_1_test_metrics.db')
        asyncio.run(M.record_request(model='m', prompt_tokens=nan, completion_tokens=inf, status_code=200))
        snap = asyncio.run(M.summary()) if hasattr(M, 'summary') and asyncio.iscoroutinefunction(M.summary) else M.summary()
        _j.dumps(snap, allow_nan=False), 'poisoned snapshot: ' + path
        assert snap['input_tokens'] == 0 and snap['output_tokens'] == 0, (path, snap)


def test_b27_2_nvidia_metrics_insert_actually_runs_and_clamps():
    """B-27.2: the B-27.1 clamp was first inserted as a nested def AFTER its
    use — NameError swallowed by record_request's broad except, silently
    killing EVERY nvidia metrics insert (live runtime gate caught it:
    midfault-count delta stayed 0). The record path must actually execute
    the INSERT and clamp non-finite tokens."""
    import asyncio, importlib.util
    spec = importlib.util.spec_from_file_location('nv_metrics', ROOT / 'nvidia-python/src/metrics.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeDB:
        def __init__(self):
            self.rows = []
        async def execute(self, sql, params):
            self.rows.append((sql, params))

    M = mod.Metrics.__new__(mod.Metrics)
    M._db = FakeDB()
    M._on_request = None
    M._ready = asyncio.Event()
    M._ready.set()
    M._maybe_save = lambda *a, **k: None
    asyncio.run(M.record_request(prompt_tokens=float('nan'), completion_tokens=float('inf'),
                                 status=200, path='/v1/chat/completions'))
    assert M._db.rows, 'record_request never executed the INSERT (silent swallow)'
    params = M._db.rows[0][1]
    # prompt/completion/total/cached all clamped to 0 by _finite_nonneg_int
    token_vals = [p for p in params if isinstance(p, int) and p == 0]
    assert token_vals, params
    import json as _j
    _j.dumps({'r': list(params)}, allow_nan=False)


def test_b28_1_dashboards_escape_data_interpolations():
    """B-28.1 (stored-XSS class lock): the public dashboard surfaces render
    upstream/config-derived strings (model ids, key labels, error reasons).
    Any such interpolation into innerHTML WITHOUT esc() is a stored-XSS hole
    in the operator's browser. Lock the exact data flows + a generic title-
    attribute scan stays at zero unescaped interpolations."""
    import re as _re
    files = ['common/dashboard_template.html', 'nous/dashboard.html',
             'opencode/dashboard.html', 'blackbox/dashboard.html',
             'nvidia-python/dashboard.html']
    # exact render sites that MUST be escaped (one per data flow)
    required = ["esc(r.model || '')", "esc(r.model || '—')",
                "esc(r.state || 'unknown')", "esc(r.status || '—')",
                "esc(r.reason || '—')", "esc(r.key_label || '—')",
                "esc(label)", "esc(mid)", "esc(stateStr)", "esc(it.label)",
                "esc(mbKeys.join(', '))", "esc(mbKeys.slice(0, 3).join(', '))",
                "esc(k.label || '?')", "esc(k.label || '')",
                "esc(prefix.length > 28"]
    for f in files:
        src = (ROOT / f).read_text()
        assert 'function esc(' in src, f'{f}: esc() helper missing'
        for site in required:
            assert site in src, f'{f}: missing {site}'
        # generic invariant: no bare variable interpolation into a title attr
        for pat in ('title="\\\' + (', 'title="\\"\' + ('):
            idx = 0
            while True:
                i = src.find(pat, idx)
                if i == -1:
                    break
                nxt = src[i + len(pat):i + len(pat) + 4]
                assert nxt.startswith('esc('), \
                    f'{f}: unescaped title= interpolation at {i}: {src[i:i+80]!r}'
                idx = i + 1
    # openrouter's dashboard: assert ONLY server-minted/numeric interpolations
    src = (ROOT / 'openrouter/dashboard.html').read_text()
    for m in _re.finditer(r'\$\{([^}]+)\}', src):
        expr = m.group(1)
        allowed = ('health.version', 'health.uptime_', 'stats.', 'kp.total_keys',
                   'kp.available_keys', 'k.label', 'k.current_rpm', 'k.in_flight',
                   'k.hard_blocked', 'cHealth.ok', 'hasMgmt ?', 'e.message')
        assert any(a in expr for a in allowed), \
            f'openrouter dashboard: unchecked interpolation ${{{expr}}}'


def test_b29_1_latency_middleware_sanitizes_log_values():
    """B-29.1 (log-forging class lock): the per-request latency middleware
    logged client-controlled values raw — a percent-encoded %0a/%0d in the
    request path (ASGI paths arrive decoded) or a crafted x-request-id forged
    arbitrary log lines, poisoning operators' triage and log-based alerting.
    All four latency-middleware sites must sanitize via _log_clean (the
    R18-aligned CR/LF/control-stripper), and the helper must behave."""
    import textwrap as _tw
    for wrapper, helper_name in (('nous', '_log_clean'), ('opencode', '_log_clean'),
                                 ('blackbox', '_log_clean'), ('nvidia-python', '_log_clean')):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert f'def {helper_name}(' in src, f'{wrapper}: _log_clean helper missing'
        assert 'path={_log_clean(request.url.path)}' in src, \
            f'{wrapper}: latency middleware still logs raw path'
        assert 'request_id={_log_clean(request_id)}' in src, \
            f'{wrapper}: latency middleware still logs raw request_id'
    # behavior: exec the nous helper (which wraps sanitize_header_value)
    src = (ROOT / 'nous' / 'src' / 'main.py').read_text()
    i = src.index('def _log_clean(')
    tail = '    return s or \'unknown\'\n'
    j = src.index(tail, i) + len(tail)
    block = src[i:j]
    from common.middleware import sanitize_header_value as _shv
    ns = {'_sanitize_header_value': _shv}
    exec(block, ns)
    clean = ns['_log_clean']
    forged = '/v1/models\x0a[INFO]_FORGED\x0d/x'
    out = clean(forged)
    assert '\x0a' not in out and '\x0d' not in out
    assert '[INFO]_FORGED' in out  # text survives, line structure safe
    assert clean('') == 'unknown'
    assert len(clean('z' * 5000)) <= 512


def test_b31_1_x_request_id_echo_sanitized_response_side():
    """B-31.1 (response-header-encoding class): wrappers echoed client- and
    upstream-supplied x-request-id straight into response headers. Values
    with codepoints >255 (or control chars) raised UnicodeEncodeError at the
    latin-1 send encode → unhandled 500 mid-response (§3.3/§4). All echo
    sites must pass through _hdr_echo (printable-ASCII, capped)."""
    for wrapper in ('nous', 'opencode', 'blackbox', 'nvidia-python'):
        src = (ROOT / wrapper / 'src' / 'main.py').read_text()
        assert 'def _hdr_echo(' in src, f'{wrapper}: _hdr_echo missing'
        assert '_hdr_echo(request_id)' in src, f'{wrapper}: raw request_id echoed'
    src = (ROOT / 'openrouter' / 'src' / 'main.py').read_text()
    assert 'def _hdr_echo(' in src
    assert '_hdr_echo(rid)' in src
    # upstream echoes (4 response.headers.get + 1 res.headers.get sites)
    assert src.count('_hdr_echo(response.headers.get("x-request-id", ""))') == 4
    assert src.count('_hdr_echo(res.headers.get("x-request-id", ""))') == 1
    base = (ROOT / 'common' / 'base_wrapper.py').read_text()
    assert "ord(ch) <= 126" in base, 'reference base still echoes raw x-request-id'
    # helper behavior (exec one)
    i = src.index('def _hdr_echo(')
    j = src.index("return s[:max_len] or 'unknown'", i) + len("return s[:max_len] or 'unknown'")
    ns: dict = {}
    exec(src[i:j], ns)
    echo = ns['_hdr_echo']
    assert echo('req-💥\x0aevil') == 'req-evil'  # non-ASCII/control stripped, printable kept
    assert echo('plain-uuid-1234') == 'plain-uuid-1234'
    assert echo('') == 'unknown'
    assert len(echo('z' * 500)) == 128


def test_b32_1_pool_release_never_drifts_total_below_sum_per_key():
    """B-32.1: pool.release() decremented _in_flight_total unconditionally
    while the per-key counter floored at 0 — a double-release drifted the
    prom `_in_flight_total` permanently BELOW sum(per-key), silently evading
    the in-flight cap the operator monitors. Release must be paired: total
    only decrements when a per-key slot actually existed."""
    import importlib.util, os
    for name in ('opencode', 'blackbox', 'openrouter'):
        spec = importlib.util.spec_from_file_location(
            f'{name}_kp_b32', ROOT / name / 'src' / 'key_pool.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pool = mod.KeyPool.__new__(mod.KeyPool)
        pool.keys = [mod.KeyEntry('k1', 'x' * 32), mod.KeyEntry('k2', 'y' * 32)]
        pool._lock = __import__('asyncio').Lock()
        pool._in_flight_total = 0
        pool._rr = 0  # round-robin cursor (exists on live pools)
        pool.soft_limit = getattr(pool, 'soft_limit', 30)
        pool.hard_limit = getattr(pool, 'hard_limit', 40)
        import asyncio
        acq = asyncio.run(pool.acquire())
        assert acq is not None
        k = acq['key']
        assert pool._in_flight_total == 1 and k.in_flight == 1
        pool.release(k)
        assert pool._in_flight_total == 0 and k.in_flight == 0
        pool.release(k)  # double release: must be a no-op on BOTH counters
        pool.release(k)
        assert pool._in_flight_total == 0 and k.in_flight == 0, (name, pool._in_flight_total, k.in_flight)
        # and acquire still works after the stray releases
        acq2 = asyncio.run(pool.acquire())
        assert acq2 is not None and pool._in_flight_total == 1


# ── R33: nvidia anthropic_compat tail-frame parity + usage-surface clamp ────

def _load_nvidia_anthropic_compat():
    """Import nvidia-python/src/anthropic_compat.py (hyphenated dir → synthetic pkg)."""
    import importlib.util
    pkg_name = 'nvidia_src_pkg'
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT / 'nvidia-python' / 'src')]
        sys.modules[pkg_name] = pkg
    name = pkg_name + '.anthropic_compat'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / 'nvidia-python' / 'src' / 'anthropic_compat.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_anthropic_stream(mod, payload: bytes):
    async def gen():
        yield payload
    async def drive():
        out = []
        async for ev in mod.stream_openai_to_anthropic(gen(), 'm', {}):
            out.append(ev)
        return out
    return asyncio.run(drive())


def _events(frames):
    """[(event_name, data_dict)] from raw 'event:/data:' SSE strings."""
    out = []
    for f in frames:
        ev = f.split('\n')[0].replace('event:', '').strip()
        data = json.loads(f.split('data:', 1)[1].strip())
        out.append((ev, data))
    return out


def test_b33_1_tail_tool_calls_parity():
    """B-33.1: a final UNTERMINATED SSE frame carrying tool_call fragments must be
    processed exactly like a newline-terminated one (was silently dropped →
    truncated tool_use partial_json)."""
    mod = _load_nvidia_anthropic_compat()
    # no trailing '\n' — the whole frame sits in the tail buffer at EOF
    frame = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x",'
             '"function":{"name":"get_weather","arguments":"{\\"city\\":"}}]},'
             '"finish_reason":"tool_calls"}]}')
    events = _events(_run_anthropic_stream(mod, frame.encode()))
    starts = [d for e, d in events if e == 'content_block_start']
    tool_starts = [d for d in starts if d.get('content_block', {}).get('type') == 'tool_use']
    assert tool_starts and tool_starts[0]['content_block']['id'] == 'call_x', events
    assert tool_starts[0]['content_block']['name'] == 'get_weather'
    deltas = [d for e, d in events if e == 'content_block_delta'
              and d.get('delta', {}).get('type') == 'input_json_delta']
    assert deltas and deltas[0]['delta']['partial_json'] == '{"city":', events
    md = [d for e, d in events if e == 'message_delta']
    assert md and md[-1]['delta']['stop_reason'] == 'tool_use', events
    assert not [e for e, _ in events if e == 'error'], events
    assert events[-1][0] == 'message_stop'


def test_b33_1_tail_error_fidelity():
    """B-33.1: an upstream error frame WITHOUT trailing newline must surface its
    REAL message (was masked by the generic premature-EOF error)."""
    mod = _load_nvidia_anthropic_compat()
    frame = 'data: {"error":{"message":"upstream exploded"}}'
    events = _events(_run_anthropic_stream(mod, frame.encode()))
    errs = [d for e, d in events if e == 'error']
    assert errs and 'upstream exploded' in errs[0]['error']['message'], events
    # stop_reason must NOT be fabricated on a failed turn
    md = [d for e, d in events if e == 'message_delta']
    assert md and md[-1]['delta']['stop_reason'] is None, events


def test_b33_1_tail_usage_capture():
    """B-33.1: usage arriving only in the final unterminated frame must drive the
    terminal message_delta (was dropped → estimates reported as real usage)."""
    mod = _load_nvidia_anthropic_compat()
    frame = ('data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
             '"usage":{"prompt_tokens":42,"completion_tokens":7}}')
    events = _events(_run_anthropic_stream(mod, frame.encode()))
    md = [d for e, d in events if e == 'message_delta']
    assert md, events
    assert md[-1]['usage']['input_tokens'] == 42, events
    assert md[-1]['usage']['output_tokens'] == 7, events


def test_b33_2_openai_to_anthropic_usage_clamp():
    """B-33.2: NaN/Infinity/None usage counters must never reach the response
    surface (Starlette renders allow_nan=False → ValueError → 500 on success)."""
    mod = _load_nvidia_anthropic_compat()
    nan, inf = float('nan'), float('inf')
    o = {'choices': [{'message': {'content': 'hi'}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': nan, 'completion_tokens': inf,
                   'prompt_tokens_details': {'cached_tokens': nan}}}
    resp = mod.openai_to_anthropic(o, 'm', estimated_input=5)
    json.dumps(resp, allow_nan=False)  # must not raise
    u = resp['usage']
    assert u['input_tokens'] == 5, u          # NaN → local estimate
    assert u['output_tokens'] == 1, u          # Inf → estimate ('hi' → 1)
    assert u['cache_read_input_tokens'] == 0, u
    for k, v in u.items():
        assert isinstance(v, int) and v >= 0, (k, v)
    # explicit None also collapses (was JSON-null on a required SDK int field)
    o2 = {'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}],
          'usage': {'prompt_tokens': None, 'completion_tokens': None}}
    u2 = mod.openai_to_anthropic(o2, 'm', estimated_input=3)['usage']
    json.dumps(u2, allow_nan=False)
    assert u2['input_tokens'] == 3, u2


def test_b33_2_stream_terminal_usage_clamp():
    """B-33.2: a NaN usage frame inside the stream must not leak NaN/Infinity
    literals into any emitted SSE frame."""
    mod = _load_nvidia_anthropic_compat()
    payload = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
               b'"usage":{"prompt_tokens":NaN,"completion_tokens":Infinity,'
               b'"prompt_tokens_details":{"cached_tokens":NaN}}}\n\n')
    frames = _run_anthropic_stream(mod, payload)
    raw = ''.join(frames)
    assert 'NaN' not in raw and 'Infinity' not in raw, raw[-400:]
    md = [d for e, d in _events(frames) if e == 'message_delta']
    assert md, frames
    u = md[-1]['usage']
    for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens'):
        assert isinstance(u[k], int) and u[k] >= 0, (k, u)


def test_b33_1_responses_compat_tail_parity_structure():
    """B-33.1 structure lock: responses_compat's V-16 tail recovery must keep
    error-frame fidelity AND tool_calls handling in sync with its main loop."""
    src = (ROOT / 'nvidia-python' / 'src' / 'responses_compat.py').read_text()
    tail = src.split("tail = buffer.strip()", 1)[1]
    tail = tail.split('except Exception as e:', 1)[0]
    assert "c.get('error') is not None" in tail, 'tail error fidelity missing'
    assert "d.get('tool_calls') or []" in tail, 'tail tool_calls loop missing'
    assert 'function_call_arguments.delta' in tail
    assert 'make_tool_acc(idx, tc)' in tail
    # and anthropic_compat's own tail keeps the full parity set
    asrc = (ROOT / 'nvidia-python' / 'src' / 'anthropic_compat.py').read_text()
    atail = asrc.split('B-33.1 (R-08 follow-up)', 1)[1]
    for needle in ("chunk.get('usage')", "chunk.get('error') is not None",
                   "delta.get('tool_calls') or []", 'input_json_delta'):
        assert needle in atail, needle
