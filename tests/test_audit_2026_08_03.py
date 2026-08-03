#!/usr/bin/env python3
"""Unit tests for the 2026-08-03 deep-audit fixes.

Covers the P0/P1 defect classes proven live against all five wrappers:

  P0-1  premature upstream EOF must surface an error, never a fabricated
        success (WRAPPER_CONTRACT §3.3) — incl. bare-[DONE]-without-
        finish_reason truncation signals
  P0-2  Authorization and x-api-key are evaluated independently (contract §5.4)
        — a stale value in one header must never mask a valid token in the
        other
  P0-3  the tool NAME must never be emitted as a function_call_arguments
        delta (delta-accumulating clients collected `name{...}` — invalid
        JSON)
  P0-4  tokenizer control tokens (`'><unk><unk>…` user report) are scrubbed
        from every client-visible text channel, incl. tokens fragmented
        across chunks
  P1-3  Responses usage objects carry the full input/output details
        scaffolding the strict openai SDK requires

Run:  pytest tests/test_audit_2026_08_03.py -q
"""

import asyncio
import json
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.auth import extract_client_tokens, check_auth  # noqa: E402

_SERVER_ENV = 'BEARER_TOKEN'
_SERVER_TOKEN = 'real-token'


def _match(headers) -> bool:
    """check_auth() with a fixed server token configured."""
    import os
    old = os.environ.get(_SERVER_ENV)
    os.environ[_SERVER_ENV] = _SERVER_TOKEN
    try:
        return bool(check_auth(headers, env_var=_SERVER_ENV))
    finally:
        if old is None:
            os.environ.pop(_SERVER_ENV, None)
        else:
            os.environ[_SERVER_ENV] = old
from common.sanitize_tokens import (  # noqa: E402
    DONE_WITHOUT_FINISH_MSG,
    PREMATURE_EOF_MSG,
    DsmlMarkupFilter,
    PassthroughBlockRewriter,
    PassthroughSSE,
    SpecialTokenFilter,
    filter_special_tokens,
    reset_caches as _st_reset_caches,
    scrub_chat_chunk_inplace,
    scrub_openai_response_inplace,
)
from common.translations.shared import responses_usage  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ────────────────────────────────────────────────────────────────────────────
# P0-2 — two-header auth (message: contract §5.4)
# ────────────────────────────────────────────────────────────────────────────

class TestAuthCandidates(unittest.TestCase):
    def test_both_headers_returned_as_candidates(self):
        hdrs = {'authorization': 'Bearer aaa', 'x-api-key': 'bbb'}
        self.assertEqual(extract_client_tokens(hdrs), ['aaa', 'bbb'])

    def test_stale_bearer_does_not_mask_valid_x_api_key(self):
        hdrs = {'authorization': 'Bearer stale-garbage', 'x-api-key': 'real-token'}
        self.assertTrue(_match(hdrs))

    def test_stale_x_api_key_does_not_mask_valid_bearer(self):
        hdrs = {'authorization': 'Bearer real-token', 'x-api-key': 'stale-garbage'}
        self.assertTrue(_match(hdrs))

    def test_both_stale_rejected(self):
        hdrs = {'authorization': 'Bearer stale-1', 'x-api-key': 'stale-2'}
        self.assertFalse(_match(hdrs))

    def test_case_insensitive_bearer_prefix(self):
        hdrs = {'Authorization': 'bEaReR real-token'}
        self.assertTrue(_match(hdrs))

    def test_empty_headers_rejected(self):
        self.assertFalse(_match({}))


# ────────────────────────────────────────────────────────────────────────────
# P0-4 — special token scrubbing
# ────────────────────────────────────────────────────────────────────────────

class TestSpecialTokenScrub(unittest.TestCase):
    def test_one_shot_scrub(self):
        self.assertEqual(filter_special_tokens('plain text'), 'plain text')
        self.assertNotIn('<unk>', filter_special_tokens('a<unk>b'))
        self.assertNotIn('<|im_start|>', filter_special_tokens('x<|im_start|>y'))
        self.assertNotIn('<s>', filter_special_tokens('p<s>q'))
        self.assertNotIn('</s>', filter_special_tokens('p</s>q'))
        self.assertNotIn('[UNK]', filter_special_tokens('m[UNK]n'))
        # U+0800 detokenization artifact (Samaritan letter)
        self.assertNotIn('ࠀ', filter_special_tokens('ok ࠀdone'))
        # fullwidth-pipe DeepSeek form
        self.assertNotIn('｜end▁of▁sentence｜',
                         filter_special_tokens('q<｜end▁of▁sentence｜>r'))

    def test_fragmented_token_across_chunks(self):
        f = SpecialTokenFilter()
        out = f.feed('Hello <un') + f.feed('k> world')
        out += f.flush()
        self.assertNotIn('<unk>', out)
        self.assertIn('Hello', out)
        self.assertIn('world', out)

    def test_legit_angle_bracket_prose_survives(self):
        # "<unk>"-shaped NATURAL text must not be nuked (only known tokens go)
        f = SpecialTokenFilter()
        out = f.feed('use a<b>c and 3 < 5 here')
        out += f.flush()
        self.assertIn('3 < 5', out)

    def test_passthrough_sse_chat_scrubs_and_tracks_terminal(self):
        scrub = PassthroughSSE()
        frames = scrub.feed({'id': 'c', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'm',
                             'choices': [{'index': 0, 'delta': {'content': 'hi <unk>x'},
                                          'finish_reason': None}]})
        self.assertEqual(len(frames), 1)
        d0 = frames[0]['choices'][0]['delta']['content']
        self.assertNotIn('<unk>', d0)
        self.assertFalse(scrub.saw_terminal)
        frames = scrub.feed({'id': 'c', 'choices': [{'index': 0, 'delta': {},
                                                     'finish_reason': 'stop'}]})
        self.assertTrue(scrub.saw_terminal)


# ────────────────────────────────────────────────────────────────────────────
# P0-1 — PassthroughBlockRewriter (shared verbatim-SSE driver)
# ────────────────────────────────────────────────────────────────────────────

def _blocks(chunks, terminal_done=True):
    rw = PassthroughBlockRewriter()
    out = []
    for c in chunks:
        out.extend(rw.feed(c))
    out.extend(rw.finish(terminal_done=terminal_done))
    return out


def _data_chunks(payloads):
    return [f'data: {json.dumps(p)}\n\n'.encode() for p in payloads]


class TestBlockRewriter(unittest.TestCase):
    def _chunk(self, content=None, finish=None):
        delta = {'content': content} if content is not None else {}
        return {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1,
                'model': 'm',
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

    def test_clean_stream_no_error_frame(self):
        out = _blocks(_data_chunks([self._chunk('hello'),
                                    self._chunk('', 'stop')]) + [b'data: [DONE]\n\n'])
        joined = b''.join(out).decode()
        self.assertIn('[DONE]', joined)
        self.assertNotIn('upstream_premature_eof', joined)
        self.assertNotIn('error', joined.lower())

    def test_eof_without_terminal_surfaces_error(self):
        out = _blocks(_data_chunks([self._chunk('cut off')]))
        joined = b''.join(out).decode()
        self.assertIn('upstream_premature_eof', joined)
        self.assertIn('[DONE]', joined)  # terminator still sent (no hang)

    def test_bare_done_without_finish_reason_surfaces_error(self):
        out = _blocks(_data_chunks([self._chunk('content')]) + [b'data: [DONE]\n\n'])
        joined = b''.join(out).decode()
        self.assertIn(DONE_WITHOUT_FINISH_MSG[:60], joined)
        # error arrives BEFORE the [DONE]
        self.assertLess(joined.index('error'), joined.index('[DONE]'))

    def test_truncated_tail_dropped_and_error_sent(self):
        # 'abrupt' mode: TCP cut mid-JSON — the partial frame must NOT reach
        # the client (it breaks strict SSE parsers).
        chunks = _data_chunks([self._chunk('partial')])
        chunks.append(b'data: {"id":"x","object":"chat.completion.chunk","TRUNC')
        out = _blocks(chunks)
        joined = b''.join(out).decode()
        self.assertNotIn('TRUNC', joined, 'truncated tail frame leaked to client')
        self.assertIn('upstream_premature_eof', joined)
        # every data: payload that IS emitted must parse as JSON
        for line in joined.split('\n'):
            line = line.strip()
            if line.startswith('data:') and 'DONE' not in line:
                json.loads(line[5:].strip())

    def test_crlf_framing(self):
        blob = b''.join(_data_chunks([self._chunk('a'), self._chunk('', 'stop')]))
        blob = blob.replace(b'\n\n', b'\r\n\r\n') + b'data: [DONE]\r\n\r\n'
        rw = PassthroughBlockRewriter()
        out = rw.feed(blob[:37])  # arbitrary split point
        out += rw.feed(blob[37:])
        out += rw.finish()
        joined = b''.join(out).decode()
        self.assertNotIn('error', joined.lower())
        self.assertIn('[DONE]', joined)

    def test_token_fragmented_across_blocks(self):
        chunks = _data_chunks([self._chunk('Hello <un'), self._chunk('k> ok'),
                               self._chunk('', 'stop')]) + [b'data: [DONE]\n\n']
        out = _blocks(chunks)
        joined = b''.join(out).decode()
        self.assertNotIn('<unk>', joined)
        self.assertIn('Hello', joined)

    def test_anthropic_shape_error_event(self):
        # Anthropic dialect passthrough (layer-2): message_stop missing → an
        # `error` EVENT (not a chat error chunk), and no synthesized [DONE].
        rw = PassthroughBlockRewriter()
        out = rw.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                      b'"delta":{"type":"text_delta","text":"hi"}}\n\n')
        out += rw.finish(terminal_done=False)
        joined = b''.join(out).decode()
        self.assertIn('event: error', joined)
        self.assertNotIn('[DONE]', joined)


# ────────────────────────────────────────────────────────────────────────────
# P1-3 — Responses usage shape (strict openai SDK)
# ────────────────────────────────────────────────────────────────────────────

class TestResponsesUsage(unittest.TestCase):
    def test_full_scaffolding(self):
        u = responses_usage(11, 7, cached_tokens=3, reasoning_tokens=2)
        self.assertEqual(u['input_tokens'], 11)
        self.assertEqual(u['input_tokens_details'], {'cached_tokens': 3})
        self.assertEqual(u['output_tokens'], 7)
        self.assertEqual(u['output_tokens_details'], {'reasoning_tokens': 2})
        self.assertEqual(u['total_tokens'], 18)

    def test_zero_values_still_have_details(self):
        u = responses_usage()
        self.assertIn('input_tokens_details', u)
        self.assertIn('output_tokens_details', u)


# ────────────────────────────────────────────────────────────────────────────
# P0-3 + P0-1 + P0-4 — common compat Responses stream translator
# ────────────────────────────────────────────────────────────────────────────

class TestCompatResponsesTranslator(unittest.TestCase):
    def _translate(self, sse_frames):
        from common.compat import translate_openai_chat_sse_to_responses

        async def gen():
            for f in sse_frames:
                yield f

        async def collect():
            out = []
            async for ev in translate_openai_chat_sse_to_responses(gen(), 'm'):
                out.append(ev)
            return out

        return _run(collect())

    @staticmethod
    def _events(frames):
        evs = []
        for f in frames:
            head = f.strip().split('\n')
            ev = head[0].replace('event: ', '') if head[0].startswith('event:') else None
            data = '\n'.join(l[5:].strip() for l in head if l.startswith('data:'))
            if data == '[DONE]':
                evs.append((ev, '[DONE]'))
            else:
                evs.append((ev, json.loads(data)))
        return evs

    def test_tool_name_never_in_arguments_delta(self):
        frames = [
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {
                'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function',
                                'function': {'name': 'search', 'arguments': ''}}]},
                'finish_reason': None}]}) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {
                'tool_calls': [{'index': 0, 'function': {'arguments': '{"q":1}'}}]},
                'finish_reason': None}]}) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {},
                                                'finish_reason': 'tool_calls'}]}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        evs = self._events(self._translate(frames))
        for ev, d in evs:
            if ev == 'response.function_call_arguments.delta':
                self.assertNotIn('search', d.get('delta', ''),
                                 'tool NAME leaked into an arguments delta '
                                 '(clients accumulate name{...} — invalid JSON)')
        done_args = [d.get('arguments', '') for ev, d in evs
                     if ev == 'response.function_call_arguments.done']
        for blob in done_args:
            json.loads(blob)  # must be valid JSON
        self.assertTrue(done_args)

    def test_premature_eof_fails_not_completes(self):
        frames = ['data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'cut'},
                                                      'finish_reason': None}]}) + '\n\n']
        evs = self._events(self._translate(frames))
        types = [d.get('type') for _e, d in evs if isinstance(d, dict)]
        self.assertIn('response.failed', types)
        self.assertNotIn('response.completed', types)
        failed = next(d for _e, d in evs if isinstance(d, dict)
                      and d.get('type') == 'response.failed')
        self.assertEqual((failed['response'].get('error') or {}).get('code'),
                         'upstream_premature_eof')

    def test_usage_accumulated_with_details(self):
        frames = [
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'ok'},
                                                'finish_reason': None}]}) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {},
                                                'finish_reason': 'stop'}],
                                   'usage': {'prompt_tokens': 11, 'completion_tokens': 7,
                                             'total_tokens': 18}}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        evs = self._events(self._translate(frames))
        completed = next(d for _e, d in evs if isinstance(d, dict)
                         and d.get('type') == 'response.completed')
        u = completed['response']['usage']
        self.assertEqual(u['input_tokens'], 11)
        self.assertEqual(u['output_tokens'], 7)
        self.assertIn('input_tokens_details', u)
        self.assertIn('output_tokens_details', u)

    def test_special_tokens_scrubbed_in_stream(self):
        frames = [
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'A<un'},
                                                'finish_reason': None}]}) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'k>B'},
                                                'finish_reason': None}]}) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {},
                                                'finish_reason': 'stop'}]}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        evs = self._events(self._translate(frames))
        visible = ''.join(d.get('delta', '') for _e, d in evs
                          if isinstance(d, dict) and d.get('type') == 'response.output_text.delta')
        done_txt = ''.join(d.get('text', '') for _e, d in evs
                           if isinstance(d, dict) and d.get('type') == 'response.output_text.done')
        self.assertNotIn('<unk>', visible)
        self.assertNotIn('<unk>', done_txt)
        self.assertIn('AB', done_txt)


# ────────────────────────────────────────────────────────────────────────────
# error path stop_reason — shared Anthropic stream state
# ────────────────────────────────────────────────────────────────────────────

class TestAnthropicErrorStopReason(unittest.TestCase):
    def test_error_frame_finishes_with_null_stop_reason(self):
        from common.translations.anthropic_stream import AnthropicStreamState
        state = AnthropicStreamState('m')
        state.translate_chunk({'choices': [{'index': 0, 'delta': {'content': 'hi'},
                                            'finish_reason': None}]})
        evs = state.translate_chunk({'error': {'type': 'api_error', 'message': 'boom'}})
        parsed = []
        for raw in evs:
            for block in raw.strip().split('\n'):
                if block.startswith('data:'):
                    parsed.append(json.loads(block[5:].strip()))
        types = [d.get('type') for d in parsed]
        self.assertIn('error', types, 'upstream error must surface as an error event')
        deltas = [d for d in parsed if d.get('type') == 'message_delta']
        self.assertTrue(deltas)
        self.assertIsNone(deltas[0]['delta'].get('stop_reason'),
                          'failed turn must not claim end_turn (fabricated success)')
        self.assertIn('message_stop', types)


class TestR9UniqueMessageIds(unittest.TestCase):
    """R9: every 'msg_*'/'chatcmpl-*' minting fallback must be unique per turn
    (bare ms timestamps collided across concurrent turns — same class as the
    R7 store-key fix), and nvidia's compat translator must never emit a
    double 'msg_msg_' prefix."""

    def test_shared_openai_to_anthropic_fallback_ids_unique(self):
        from common.translations.shared import openai_to_anthropic_response
        resp = {'choices': [{'message': {'content': 'hi'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
        ids = {openai_to_anthropic_response(resp, 'm')['id'] for _ in range(50)}
        self.assertEqual(len(ids), 50, 'msg ids collide across sequential mints')
        self.assertTrue(all(i.startswith('msg_') for i in ids))

    def test_shared_stream_anthropic_to_openai_id_unique(self):
        from common.translations.shared import new_response_id
        # the stream translator mints chatcmpl-<ms>-<hex> inline; assert the
        # same instantiation pattern cannot collide within a millisecond.
        ids = {new_response_id('chatcmpl') for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_anthropic_stream_state_msg_id_unique(self):
        from common.translations.anthropic_stream import AnthropicStreamState
        ids = {AnthropicStreamState('m').msg_id for _ in range(50)}
        self.assertEqual(len(ids), 50, 'msg_id collides within a millisecond')

    def test_dsml_recovered_tool_use_ids_unique(self):
        """R10: DSML-recovered/missing-id tool_use mints must be unique beyond
        one millisecond — colliding ids corrupt tool_result pairing in stored
        and replayed histories."""
        from common.translations.shared import parse_dsml_from_text
        markup = ('noise |DSML|tool_calls>|DSML|invoke name="get_weather">'
                  '|DSML|parameter name="city" string="true">Paris</|DSML|parameter>'
                  '</|DSML|invoke></|DSML|tool_calls> tail')
        ids = set()
        for _ in range(50):
            _clean, tools = parse_dsml_from_text(markup)
            self.assertEqual(len(tools), 1)
            ids.add(tools[0]['id'])
        self.assertEqual(len(ids), 50, 'DSML tool_use ids collide across turns')
        self.assertTrue(all(i.startswith('toolu_dsml_') for i in ids))

    def test_stream_tool_call_fallback_ids_unique(self):
        from common.translations.anthropic_stream import AnthropicStreamState
        ids = set()
        for _ in range(50):
            st = AnthropicStreamState('m')
            evs = st.translate_chunk({'choices': [{'index': 0, 'delta': {
                'tool_calls': [{'index': 0, 'function': {'name': 'f', 'arguments': '{'}}]},
                'finish_reason': None}]})
            starts = [e for e in evs if e.startswith('event: content_block_start')]
            self.assertTrue(starts)
            payload = json.loads(starts[0].split('data: ', 1)[1])
            ids.add(payload['content_block']['id'])
        self.assertEqual(len(ids), 50, 'fallback tool_use ids collide across turns')

    def test_nvidia_compat_single_prefix_and_unique(self):
        import importlib
        sys.path.insert(0, str(ROOT / 'nvidia-python'))
        for name in ('src.anthropic_compat', 'src'):
            sys.modules.pop(name, None)
        try:
            mod = importlib.import_module('src.anthropic_compat')
            resp = {'choices': [{'message': {'content': 'hi'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            # historical bug: caller passed 'msg_<epoch_secs>' → id came back
            # 'msg_msg_<epoch_secs>' (double prefix, non-unique)
            self.assertEqual(mod.openai_to_anthropic(resp, 'm', request_id='msg_abc')['id'], 'msg_abc')
            self.assertEqual(mod.openai_to_anthropic(resp, 'm', request_id='rawtoken')['id'], 'msg_rawtoken')
            ids = {mod.openai_to_anthropic(resp, 'm')['id'] for _ in range(50)}
            self.assertEqual(len(ids), 50, 'nvidia msg ids collide')
            self.assertFalse(any(i.startswith('msg_msg_') for i in ids))
        finally:
            sys.path.remove(str(ROOT / 'nvidia-python'))
            for name in [n for n in sys.modules if n == 'src' or n.startswith('src.')]:
                sys.modules.pop(name, None)


# ────────────────────────────────────────────────────────────────────────────
# CONTRACT §6.3 — response store bounded on all three axes
# ────────────────────────────────────────────────────────────────────────────

class TestNvidiaResponseStore(unittest.TestCase):
    def _load_mod(self):
        import importlib
        # nvidia-python ships `src` as a package root (src.responses_compat).
        sys.path.insert(0, str(ROOT / 'nvidia-python'))
        for name in ('src.responses_compat', 'src.anthropic_compat', 'src'):
            sys.modules.pop(name, None)
        try:
            return importlib.import_module('src.responses_compat')
        finally:
            sys.path.remove(str(ROOT / 'nvidia-python'))

    def setUp(self):
        self.mod = self._load_mod()
        self.mod._RESPONSE_STORE.clear()

    def test_entry_count_axis(self):
        n = self.mod._STORE_MAX_ENTRIES + 5
        for i in range(n):
            self.mod._bounded_store('p', f'r{i}', [{'role': 'user', 'content': f'q{i}'}])
        self.assertLessEqual(len(self.mod._RESPONSE_STORE), self.mod._STORE_MAX_ENTRIES)

    def test_byte_axis(self):
        # Entries of ~60% of the byte budget: storing several must evict the
        # oldest until the total fits (the most recent entry is always kept —
        # by design the newest oversized entry survives so the current turn
        # is never dropped).
        cap = self.mod._STORE_MAX_BYTES
        chunk = 'x' * int(cap * 0.6)
        for i in range(3):
            self.mod._bounded_store('p', f'r{i}', [{'role': 'user', 'content': chunk}])
        total = sum(v['size'] for v in self.mod._RESPONSE_STORE.values())
        self.assertLessEqual(total, cap)
        # newest entry survived
        self.assertIn(self.mod._store_key('p', 'r2'), self.mod._RESPONSE_STORE)

    def test_ttl_axis_store_and_load(self):
        self.mod._bounded_store('p', 'old', [{'role': 'user', 'content': 'q'}])
        # Age the entry beyond the TTL.
        key = self.mod._store_key('p', 'old')
        self.mod._RESPONSE_STORE[key]['ts'] = time.time() - self.mod._STORE_TTL_SEC - 5
        self.assertIsNone(self.mod._load_stored(key))
        self.assertNotIn(key, self.mod._RESPONSE_STORE)
        # A fresh entry survives and round-trips.
        self.mod._bounded_store('p', 'fresh', [{'role': 'user', 'content': 'q2'}])
        self.assertEqual(self.mod._load_stored(self.mod._store_key('p', 'fresh'))[0]
                         ['content'], 'q2')

    def _pop_src_modules(self):
        # `src.main` / `src.metrics` are PER-WRAPPER module names; a sibling
        # test that already imported wrapper X's `src` must not leak it into
        # this wrapper's import (python caches `src.metrics` globally).
        for name in [n for n in sys.modules if n == 'src' or n.startswith('src.')]:
            sys.modules.pop(name, None)

    def test_round4_opencode_total_store_axis(self):
        """CONTRACT §6.3 axis 3: opencode's store was bounded per CONVERSATION
        only — 200 entries × per-entry trim could still hold ~100 MB. The
        whole store must fit RESPONSES_STORE_MAX_BYTES, evicting OLDEST first
        so the freshest turn always survives (blackbox got the same fix for
        its LIFO byte eviction direction)."""
        sys.path.insert(0, str(ROOT / 'opencode'))
        self._pop_src_modules()
        try:
            import importlib
            mod = importlib.import_module('src.main')
            mod._RESPONSE_STORE.clear()
            # Shrink the whole-store byte budget so the axis engages with small
            # entries (per-item trim is 500 KB and would otherwise swallow the
            # payload before store-total accounting ever mattered).
            saved_chars, saved_entry = (mod._RESPONSE_STORE_MAX_CHARS,
                                        mod._RESPONSE_STORE_MAX_ENTRY_CHARS)
            mod._RESPONSE_STORE_MAX_CHARS = 3000
            mod._RESPONSE_STORE_MAX_ENTRY_CHARS = 5000
            try:
                chunk = 'x' * 1200  # each stored turn ≈ 40% of the budget
                for i in range(5):  # 5 × 40% = 200% of the budget
                    mod._store_response('p', f'r{i}',
                                        [{'role': 'user', 'content': chunk}])
                total = sum(v.get('size', 0) for v in mod._RESPONSE_STORE.values())
                self.assertLessEqual(total, 3000,
                                     'opencode store exceeds RESPONSES_STORE_MAX_BYTES in total')
                # newest entries survived, oldest were evicted
                self.assertNotIn('p\x00r0', mod._RESPONSE_STORE)
                self.assertNotIn('p\x00r1', mod._RESPONSE_STORE)
                self.assertIn('p\x00r4', mod._RESPONSE_STORE)
                # round-trip still works through _load_response
                self.assertTrue(mod._load_response('p', 'r4'))
            finally:
                mod._RESPONSE_STORE_MAX_CHARS = saved_chars
                mod._RESPONSE_STORE_MAX_ENTRY_CHARS = saved_entry
        finally:
            sys.path.remove(str(ROOT / 'opencode'))
            self._pop_src_modules()

    def test_round4_blackbox_byte_eviction_oldest_first(self):
        """blackbox byte-budget eviction was LIFO (popitem): under pressure it
        evicted the just-written entry, breaking the very next
        previous_response_id hop while ancient histories survived."""
        sys.path.insert(0, str(ROOT / 'blackbox'))
        self._pop_src_modules()
        try:
            import importlib
            mod = importlib.import_module('src.main')
            mod._RESPONSE_STORE.clear()
            saved = mod._RESPONSE_STORE_MAX_BYTES
            mod._RESPONSE_STORE_MAX_BYTES = 3000
            try:
                chunk = 'y' * 1800  # each stored turn ≈ 60% of the budget
                for i in range(3):  # 3 × 60% = 180% of the budget
                    mod._store_response('p', f'r{i}',
                                        [{'role': 'user', 'content': chunk}])
                total = sum(v[1] for v in mod._RESPONSE_STORE.values() if isinstance(v, tuple))
                self.assertLessEqual(total, 3000,
                                     'blackbox store exceeds RESPONSES_STORE_MAX_BYTES in total')
                self.assertIn(mod._response_store_key('p', 'r2'), mod._RESPONSE_STORE)
                self.assertNotIn(mod._response_store_key('p', 'r0'), mod._RESPONSE_STORE)
            finally:
                mod._RESPONSE_STORE_MAX_BYTES = saved
        finally:
            sys.path.remove(str(ROOT / 'blackbox'))
            self._pop_src_modules()

    def test_canonical_defaults(self):
        self.assertEqual(self.mod._STORE_MAX_ENTRIES, 200)
        self.assertEqual(self.mod._STORE_MAX_BYTES, 33554432)
        self.assertEqual(self.mod._STORE_TTL_SEC, 3600)


class TestStoreDeepCopyIsolation(unittest.TestCase):
    """R8 (N-19 parity, all 5 wrappers): the Responses conversation store must
    be isolated from LIVE request objects in BOTH directions —
    (a) mutating the caller's message dicts AFTER store_conversation must not
        corrupt the stored history, and
    (b) mutating the dicts RETURNED by a replay load must not corrupt the
        stored entry (or concurrent replays of the same response id).
    Without this, two concurrent agents sharing a principal can poison each
    other's replayed history through normalisation/sanitisation pipelines
    that edit message dicts in place."""

    WRAPPERS = ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter')

    def _pop_src_modules(self):
        for name in [n for n in sys.modules if n == 'src' or n.startswith('src.')]:
            sys.modules.pop(name, None)

    def _store_and_load(self, wrapper):
        """Return (store_fn, load_fn) adapters with a uniform interface."""
        import importlib
        sys.path.insert(0, str(ROOT / wrapper))
        self._pop_src_modules()
        try:
            if wrapper == 'nvidia-python':
                mod = importlib.import_module('src.responses_compat')
                mod._RESPONSE_STORE.clear()

                def store(p, rid, msgs):
                    mod._bounded_store(p, rid, msgs)

                def load(p, rid):
                    return mod._load_stored(mod._store_key(p, rid))

            elif wrapper == 'nous':
                mod = importlib.import_module('src.main')
                mod._RESPONSE_STORE.clear()

                def store(p, rid, msgs):
                    asyncio.run(mod.store_conversation(p, rid, msgs))

                def load(p, rid):
                    return mod.get_stored_conversation(p, rid)

            elif wrapper == 'opencode':
                mod = importlib.import_module('src.main')
                mod._RESPONSE_STORE.clear()

                def store(p, rid, msgs):
                    mod._store_response(p, rid, msgs)

                def load(p, rid):
                    return mod._load_response(p, rid)

            elif wrapper in ('blackbox', 'openrouter'):
                mod = importlib.import_module('src.main')
                mod._RESPONSE_STORE.clear()

                def store(p, rid, msgs):
                    mod._store_response(p, rid, msgs)

                def load(p, rid):
                    return mod._get_stored_conversation(p, rid) or None

            return store, load
        finally:
            sys.path.remove(str(ROOT / wrapper))
            self._pop_src_modules()

    def test_all_wrappers_store_isolated_both_directions(self):
        for wrapper in self.WRAPPERS:
            with self.subTest(wrapper=wrapper):
                store, load = self._store_and_load(wrapper)
                live = [{'role': 'user', 'content': [{'type': 'text', 'text': 'orig'}]},
                        {'role': 'assistant', 'content': 'ans', 'tool_calls': [
                            {'id': 'c1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{"a": 1}'}}]}]
                store('p', 'rid', live)
                # (a) mutate the caller's live dicts AFTER the store write —
                # a real pipeline does this (normalisation, content coercion).
                live[0]['content'][0]['text'] = 'MUTATED-AFTER-STORE'
                live[1]['tool_calls'][0]['function']['arguments'] = '{"hacked": true}'
                loaded = load('p', 'rid')
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[0]['content'][0]['text'], 'orig',
                                 f'{wrapper}: store corrupted by post-write mutation')
                self.assertEqual(loaded[1]['tool_calls'][0]['function']['arguments'],
                                 '{"a": 1}',
                                 f'{wrapper}: tool_calls corrupted by post-write mutation')
                # (b) mutate the REPLAYED copy — the stored entry must survive,
                # so a concurrent second replay sees pristine data.
                loaded[0]['content'][0]['text'] = 'MUTATED-REPLAY'
                loaded[1]['content'] = {'totally': 'rewritten'}
                again = load('p', 'rid')
                self.assertEqual(again[0]['content'][0]['text'], 'orig',
                                 f'{wrapper}: store corrupted by replay-side mutation')
                self.assertEqual(again[1]['content'], 'ans',
                                 f'{wrapper}: store corrupted by replay-side mutation')

    def test_nous_openrouter_store_axes_bounded(self):
        """R8 parity lock: nous + openrouter stores honour the entry-count
        axis (CONTRACT §6.3), same as the nvidia/opencode/blackbox tests
        above. Openrouter uses an OrderedDict LRU; nous prunes oldest-first
        under its async lock."""

        async def _fill(mod):
            for i in range(mod._RESPONSE_STORE_MAX + 10):
                await mod.store_conversation(
                    'p', f'r{i}', [{'role': 'user', 'content': f'q{i}'}])

        # nous (async store path)
        sys.path.insert(0, str(ROOT / 'nous'))
        self._pop_src_modules()
        try:
            import importlib
            mod = importlib.import_module('src.main')
            mod._RESPONSE_STORE.clear()
            asyncio.run(_fill(mod))
            self.assertLessEqual(len(mod._RESPONSE_STORE),
                                 mod._RESPONSE_STORE_MAX,
                                 'nous store exceeds entry-count axis')
            self.assertIn(mod._response_store_key('p', f'r{mod._RESPONSE_STORE_MAX + 9}'),
                          mod._RESPONSE_STORE,
                          'nous store evicted the FRESHEST entry')
        finally:
            sys.path.remove(str(ROOT / 'nous'))
            self._pop_src_modules()

        # openrouter (sync store path, OrderedDict LRU)
        sys.path.insert(0, str(ROOT / 'openrouter'))
        self._pop_src_modules()
        try:
            import importlib
            mod = importlib.import_module('src.main')
            mod._RESPONSE_STORE.clear()
            for i in range(mod._RESPONSE_STORE_MAX_ENTRIES + 10):
                mod._store_response('p', f'r{i}', [{'role': 'user', 'content': f'q{i}'}])
            self.assertLessEqual(len(mod._RESPONSE_STORE),
                                 mod._RESPONSE_STORE_MAX_ENTRIES,
                                 'openrouter store exceeds entry-count axis')
            self.assertIn(mod._response_store_key('p', f'r{mod._RESPONSE_STORE_MAX_ENTRIES + 9}'),
                          mod._RESPONSE_STORE,
                          'openrouter store evicted the FRESHEST entry')
        finally:
            sys.path.remove(str(ROOT / 'openrouter'))
            self._pop_src_modules()


class TestOpencodeStoreEnvNames(unittest.TestCase):
    def test_canonical_env_names_present(self):
        src = (ROOT / 'opencode' / 'src' / 'main.py').read_text()
        for env in ('RESPONSES_STORE_MAX_ENTRIES', 'RESPONSES_STORE_MAX_BYTES',
                    'RESPONSES_STORE_TTL_SEC'):
            self.assertIn(env, src, f'opencode store missing canonical env {env}')
        # and the hard-coded 200 limit is gone from the eviction path
        for fi in ('blackbox', 'openrouter', 'nous'):
            src2 = (ROOT / fi / 'src' / 'main.py').read_text()
            self.assertIn('RESPONSES_STORE_MAX_ENTRIES', src2)


# ────────────────────────────────────────────────────────────────────────────
# P0-5 (round-4) — JSONBodyGuard: JSON-null hole, semantic validation, and the
# Content-Type bypass. Valid-JSON broken-semantics bodies detonated
# AttributeErrors inside handlers on ALL 8 covered crash sites (500/502) in
# every wrapper: messages list-of-str, tools list-of-str, model as number,
# content blocks as bare strings, scalar-null bodies.
# ────────────────────────────────────────────────────────────────────────────

from common.body_guard import JSONBodyGuard


def _drive_guard(raw_body, path='/v1/chat/completions', method='POST',
                 content_type=b'application/json'):
    """Run the JSONBodyGuard ASGI middleware against a recording inner app.

    Returns (status_or_None, response_body_bytes, app_received_body_or_None).
    status None means the guard passed the request through to the inner app.
    """
    import asyncio as _asyncio

    sent = []
    app_state = {'received': None}

    async def inner_app(scope, receive, send):
        body = b''
        while True:
            msg = await receive()
            if msg['type'] == 'http.request':
                body += msg.get('body', b'')
                if not msg.get('more_body'):
                    break
            elif msg['type'] == 'http.disconnect':
                body = None
                break
        app_state['received'] = body

    async def receive():
        return {'type': 'http.request', 'body': raw_body, 'more_body': False}

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'method': method, 'path': path,
             'headers': [(b'content-type', content_type)] if content_type else []}
    guard = JSONBodyGuard(inner_app)
    _asyncio.new_event_loop().run_until_complete(guard(scope, receive, send))

    status = None
    body = b''
    for m in sent:
        if m['type'] == 'http.response.start':
            status = m['status']
        elif m['type'] == 'http.response.body':
            body += m.get('body', b'')
    return status, body, app_state['received']


class TestBodyGuardRound4(unittest.TestCase):
    def assertRejected(self, raw, path='/v1/chat/completions', ctype=b'application/json',
                       expect_status=400):
        status, body, received = _drive_guard(raw, path, content_type=ctype)
        self.assertEqual(status, expect_status, f'{raw!r} on {path} was not rejected')
        self.assertIsNone(received, 'inner app must not observe a rejected body')
        env = json.loads(body.decode())
        self.assertIn('error', env)
        if path.startswith('/v1/messages'):
            self.assertEqual(env.get('type'), 'error')  # Anthropic envelope
        return env

    def assertPassed(self, raw, path='/v1/chat/completions', ctype=b'application/json'):
        status, _body, received = _drive_guard(raw, path, content_type=ctype)
        self.assertIsNone(status, f'{raw!r} on {path} unexpectedly rejected')
        self.assertEqual(received, raw)

    # 1. The JSON-null hole: json.loads('null') is None, which previously took
    #    the same pass-through branch as a parse failure → 500 on all 5 wrappers.
    def test_r4_json_null_rejected_everywhere(self):
        for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses',
                     '/v1/messages/count_tokens', '/v1/embeddings'):
            self.assertRejected(b'null', path)

    # 2. Non-object bodies remain rejected (previous behaviour preserved).
    def test_r4_non_objects_still_rejected(self):
        for raw in (b'[1,2,3]', b'"str"', b'42', b'true'):
            self.assertRejected(raw, '/v1/chat/completions')

    # 3. Semantic validation: messages must be a list of objects.
    def test_r4_messages_semantics(self):
        self.assertRejected(json.dumps({'messages': ['hello', 42, None]}).encode())
        self.assertRejected(json.dumps({'messages': 'just a string'}).encode())
        self.assertRejected(json.dumps({'messages': {'role': 'user'}}).encode())
        self.assertRejected(json.dumps({'messages': [None]}).encode())
        # Anthropic surface gets the Anthropic envelope
        env = self.assertRejected(
            json.dumps({'messages': ['x']}).encode(), '/v1/messages')
        self.assertEqual(env['error']['type'], 'invalid_request_error')

    # 4. Content blocks / tool_calls items must be objects.
    def test_r4_nested_items_semantics(self):
        self.assertRejected(json.dumps(
            {'messages': [{'role': 'user', 'content': ['x', None, 7]}]}).encode())
        self.assertRejected(json.dumps(
            {'messages': [{'role': 'assistant',
                           'tool_calls': ['x', {'id': 1}]}]}).encode())
        self.assertRejected(json.dumps({'system': [{'type': 'text', 'text': 's'}, 5],
                                        'messages': []}).encode(), '/v1/messages')

    # 5. tools must be a list of objects (both crash sites in translators).
    def test_r4_tools_semantics(self):
        self.assertRejected(json.dumps(
            {'messages': [{'role': 'user', 'content': 'hi'}], 'tools': ['x', None]}).encode())
        self.assertRejected(json.dumps(
            {'messages': [{'role': 'user', 'content': 'hi'}], 'tools': {'name': 'a'}}).encode())

    # 6. max_tokens contract §4: positive int ≤ 1_000_000, bool excluded.
    def test_r4_max_tokens_semantics(self):
        base = {'messages': [{'role': 'user', 'content': 'hi'}]}
        for bad in ('abc', True, -5, 0, 999999999999, 3.7):
            with self.subTest(max_tokens=bad):
                self.assertRejected(json.dumps({**base, 'max_tokens': bad}).encode())
        self.assertRejected(json.dumps({'input': 'hi', 'max_output_tokens': 'lots'}).encode(),
                            '/v1/responses')
        self.assertRejected(json.dumps({'input': 'hi', 'max_output_tokens': 0}).encode(),
                            '/v1/responses')

    # 7. model must be a string (crashed nvidia resolve_target_model).
    def test_r4_model_semantics(self):
        self.assertRejected(json.dumps({'model': 42, 'messages': []}).encode())
        self.assertRejected(json.dumps({'model': {'name': 'x'}, 'messages': []}).encode())

    # 8. Responses input: string or list-of-objects only.
    def test_r4_responses_input_semantics(self):
        self.assertRejected(b'{"model": "m", "input": 12345}', '/v1/responses')
        self.assertRejected(b'{"model": "m", "input": ["hi", null]}', '/v1/responses')
        self.assertPassed(b'{"model": "m", "input": "hi"}', '/v1/responses')
        self.assertPassed(b'{"model": "m", "input": [{"role": "user", "content": "hi"}]}',
                          '/v1/responses')

    # 9. Content-Type bypass: text/plain JSON used to skip ALL inspection.
    def test_r4_content_type_bypass_closed(self):
        raw = json.dumps({'messages': ['hello']}).encode()
        self.assertRejected(raw, '/v1/chat/completions', ctype=b'text/plain')
        self.assertRejected(b'null', '/v1/chat/completions', ctype=b'text/plain')

    # 10. Management surfaces keep owning their body shapes.
    def test_r4_non_inference_paths_untouched(self):
        self.assertPassed(json.dumps({'messages': ['x']}).encode(),
                          '/openrouter/keys/create')
        # …but the object-shape rule still applies globally.
        self.assertRejected(b'[1,2,3]', '/openrouter/keys/create')

    # 11. Valid bodies pass through BYTE-FOR-BYTE (replay fidelity).
    def test_r4_valid_bodies_pass(self):
        valid = {
            'model': 'mock/normal', 'max_tokens': 64, 'stream': True,
            'messages': [
                {'role': 'system', 'content': [{'type': 'text', 'text': 's'}]},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'hi'},
                    {'type': 'image', 'source': {'type': 'base64', 'data': 'zz'}}]},
                {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_1', 'type': 'function',
                     'function': {'name': 'f', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'r'},
            ],
            'tools': [{'type': 'function', 'function': {'name': 'f', 'parameters': {}}}],
        }
        raw = json.dumps(valid, ensure_ascii=False).encode()
        self.assertPassed(raw, '/v1/chat/completions')
        self.assertPassed(raw, '/v1/messages')
        self.assertPassed(b'{"model": "m", "input": "hi", "store": false}', '/v1/responses')
        # empty body and garbage JSON pass through to the route's own 400
        self.assertPassed(b'', '/v1/chat/completions')
        self.assertPassed(b'{not json', '/v1/chat/completions')


# ── R5 double-scrub fix: dsml_suppress flag on the shared passthrough ────
_DSML_STREAM = ('<|DSML|tool_calls>\n<|DSML|invoke name="get_weather">\n'
                '<|DSML|parameter name="city" string="true">Jakarta</|DSML|parameter>\n'
                '</|DSML|invoke>\n</|DSML|tool_calls>')


def _chat_sse_bytes(deltas, finish='stop'):
    out = []
    for i, d in enumerate(deltas):
        obj = {'id': 'chatcmpl-1', 'object': 'chat.completion.chunk', 'created': 1,
               'model': 'm',
               'choices': [{'index': 0, 'delta': {'content': d},
                            'finish_reason': finish if i == len(deltas) - 1 else None}]}
        out.append(f'data: {json.dumps(obj)}\n\n'.encode())
    out.append(b'data: [DONE]\n\n')
    return out


def _parse_anthropic_sse(frames):
    evts = []
    buf = ''
    for fr in frames:
        buf += fr if isinstance(fr, str) else fr.decode()
    for block in buf.split('\n\n'):
        if not block.strip():
            continue
        ev = None
        data = None
        for line in block.split('\n'):
            if line.startswith('event:'):
                ev = line[6:].strip()
            elif line.startswith('data:'):
                data = line[5:].strip()
        if data:
            try:
                evts.append((ev, json.loads(data)))
            except ValueError:
                pass
    return evts


async def _agen(items):
    for it in items:
        yield it


class TestR5DsmlSuppressFlag(unittest.TestCase):
    """R5 runtime finding ("double-scrub"): the shared passthrough rewriter
    stripped MiniMax DSML tool markup BEFORE the /v1/messages translator
    could recover it, silently losing the tool call. dsml_suppress=False
    keeps markup intact for the recovering translator; default stays True
    (suppress-only surfaces)."""

    def _run_rewriter(self, rewriter):
        frags = ['Prose before. ', '<|DS', 'ML|tool_calls>\n<|DSML|invoke name="get_weather">\n',
                 '<|DSML|parameter name="city" string="true">Jakarta</|DSML|parameter>\n',
                 '</|DSML|invoke>\n</|DSML|tool_calls>', ' prose after.']
        out = b''
        for c in _chat_sse_bytes(frags):
            for f in rewriter.feed(c):
                out += f
        for f in rewriter.finish(terminal_done=False):
            out += f
        return out.decode()

    def test_default_suppresses_markup(self):
        txt = self._run_rewriter(PassthroughBlockRewriter())
        self.assertNotIn('DSML', txt)
        self.assertNotIn('invoke name', txt)
        self.assertIn('Prose before.', txt)
        self.assertIn('prose after.', txt)

    def test_suppress_off_passes_markup_through(self):
        txt = self._run_rewriter(PassthroughBlockRewriter(dsml_suppress=False))
        self.assertIn('DSML', txt)
        self.assertIn('get_weather', txt)

    def test_passthrough_sse_flag(self):
        sse = PassthroughSSE(dsml_suppress=False)
        self.assertIsNone(sse._dsml_text)
        obj = {'id': 'c', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm',
               'choices': [{'index': 0, 'delta': {'content': '<|DSML|to'}, 'finish_reason': None}]}
        frames = sse.feed(obj)
        # markup survives DSML suppression; the special-token filter may only
        # hold a short possible-prefix tail, released intact on flush.
        emitted = ''.join(f['choices'][0]['delta'].get('content', '') for f in frames)
        flushed = ''.join(f.get('choices', [{}])[0].get('delta', {}).get('content', '')
                          for f in sse.flush())
        self.assertEqual(emitted + flushed, '<|DSML|to')


class TestR5TranslatorRecovery(unittest.TestCase):
    """R5: the /v1/messages translators recover fragmented DSML markup as
    real tool_use blocks (never leaking protocol text), with stop_reason
    upgraded to tool_use (MiniMax reports finish 'stop' for those turns)."""

    _FRAGS = ['Prose before. ', '<|DS', 'ML|tool_calls>\n<|DSML|invoke name="get_weather">\n',
              '<|DSML|parameter name="city" string="true">Jakarta</|DSML|parameter>\n',
              '</|DSML|invoke>\n</|DSML|tool_calls>', ' prose after.']

    def _load(self, wrapper, modname):
        import importlib
        for m in [k for k in list(sys.modules) if k == 'src' or k.startswith('src.')]:
            del sys.modules[m]
        p = str(ROOT / wrapper)
        sys.path.insert(0, p)
        try:
            return importlib.import_module(modname)
        finally:
            sys.path.remove(p)

    def test_nvidia_stream_recovers_tool_use(self):
        nv = self._load('nvidia-python', 'src.anthropic_compat')

        async def run():
            return [fr async for fr in nv.stream_openai_to_anthropic(
                _agen(_chat_sse_bytes(self._FRAGS)), 'm')]

        evts = _parse_anthropic_sse(asyncio.run(run()))
        text = ''.join(d.get('delta', {}).get('text', '') for _, d in evts
                       if _ == 'content_block_delta' and d.get('delta', {}).get('type') == 'text_delta')
        tools = [d for _, d in evts if _ == 'content_block_start'
                 and d.get('content_block', {}).get('type') == 'tool_use']
        args = ''.join(d.get('delta', {}).get('partial_json', '') for _, d in evts
                       if _ == 'content_block_delta' and d.get('delta', {}).get('type') == 'input_json_delta')
        stop = next((d.get('delta', {}).get('stop_reason') for _, d in evts if _ == 'message_delta'), None)
        self.assertNotIn('DSML', text)
        self.assertNotIn('invoke', text)
        self.assertEqual([t['content_block'].get('name') for t in tools], ['get_weather'])
        self.assertIn('Jakarta', args)
        self.assertEqual(stop, 'tool_use')

    def test_nvidia_nonstream_drops_incomplete_markup(self):
        nv = self._load('nvidia-python', 'src.anthropic_compat')
        resp = {'id': 'x', 'object': 'chat.completion', 'created': 1, 'model': 'm',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant',
                                         'content': 'Hello. <|DSML|tool_calls>\n<|DSML|invoke name="f">\nTRUNCATED'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 5}}
        out = nv.openai_to_anthropic(resp, 'm')
        text = ''.join(b.get('text', '') for b in out.get('content', []) if b.get('type') == 'text')
        self.assertNotIn('DSML', text)
        self.assertNotIn('invoke', text)
        self.assertIn('Hello.', text)

    def test_openrouter_nonstream_recovers_tool_use(self):
        orr = self._load('openrouter', 'src.main')
        resp = {'id': 'x', 'object': 'chat.completion', 'created': 1, 'model': 'm',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': 'Let me check. ' + _DSML_STREAM}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 5}}
        out = orr._openai_to_anthropic_response(resp, {'model': 'm'})
        blocks = out.get('content', [])
        tools = [b for b in blocks if b.get('type') == 'tool_use']
        text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        self.assertEqual([t.get('name') for t in tools], ['get_weather'])
        self.assertEqual(tools[0].get('input', {}).get('city'), 'Jakarta')
        self.assertNotIn('DSML', text)
        self.assertEqual(out.get('stop_reason'), 'tool_use')

    def test_openrouter_stream_recovers_tool_use(self):
        orr = self._load('openrouter', 'src.main')

        async def run():
            return [fr async for fr in orr._translate_openai_stream_to_anthropic(
                _agen(_chat_sse_bytes(self._FRAGS)), {'model': 'm'})]

        evts = _parse_anthropic_sse(asyncio.run(run()))
        text = ''.join(d.get('delta', {}).get('text', '') for _, d in evts
                       if _ == 'content_block_delta' and d.get('delta', {}).get('type') == 'text_delta')
        tools = [d for _, d in evts if _ == 'content_block_start'
                 and d.get('content_block', {}).get('type') == 'tool_use']
        stop = next((d.get('delta', {}).get('stop_reason') for _, d in evts if _ == 'message_delta'), None)
        self.assertNotIn('DSML', text)
        self.assertNotIn('invoke', text)
        self.assertEqual([t['content_block'].get('name') for t in tools], ['get_weather'])
        self.assertEqual(stop, 'tool_use')



class TestR5DsmlStopReasonUpgrade(unittest.TestCase):
    """R5 follow-up: DSML-recovered tool_use blocks must upgrade stop_reason
    to tool_use even when upstream reports finish 'stop' (the MiniMax signal
    — it does not know its markup is a tool protocol). Applies to the shared
    non-stream translator AND every per-wrapper fork (CONTRACT §8 parity);
    B-06 strict mapping stays for REAL tool_calls with finish 'stop'."""

    def _resp(self):
        return {'id': 'x', 'object': 'chat.completion', 'created': 1, 'model': 'm',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': 'Checking. ' + _DSML_STREAM}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 5}}

    def _load(self, wrapper, modname):
        import importlib
        for m in [k for k in list(sys.modules) if k == 'src' or k.startswith('src.')]:
            del sys.modules[m]
        pth = str(ROOT / wrapper)
        sys.path.insert(0, pth)
        try:
            return importlib.import_module(modname)
        finally:
            sys.path.remove(pth)

    def test_shared_nonstream_upgrades_stop_reason(self):
        from common.translations import openai_to_anthropic_response
        out = openai_to_anthropic_response(self._resp(), 'm')
        self.assertEqual(out.get('stop_reason'), 'tool_use')
        tools = [b for b in out.get('content', []) if b.get('type') == 'tool_use']
        self.assertEqual([t.get('name') for t in tools], ['get_weather'])

    def test_shared_nonstream_real_tool_calls_finish_stop_stays_end_turn(self):
        # B-06 guard: REAL tool_calls with finish 'stop' keep end_turn — the
        # upgrade fires only for DSML-recovered tools.
        from common.translations import openai_to_anthropic_response
        r = self._resp()
        r['choices'][0]['message'] = {'role': 'assistant', 'content': 'x',
                                      'tool_calls': [{'id': 'c1', 'type': 'function',
                                                      'function': {'name': 'f', 'arguments': '{}'}}]}
        out = openai_to_anthropic_response(r, 'm')
        self.assertEqual(out.get('stop_reason'), 'end_turn')

    def test_nous_local_fork_upgrades(self):
        m = self._load('nous', 'src.main')
        out = m.openai_to_anthropic('m', self._resp())
        self.assertEqual(out.get('stop_reason'), 'tool_use')

    def test_opencode_local_fork_upgrades(self):
        m = self._load('opencode', 'src.main')
        out = m.openai_to_anthropic('m', self._resp())
        self.assertEqual(out.get('stop_reason'), 'tool_use')

    def test_blackbox_local_fork_upgrades(self):
        m = self._load('blackbox', 'src.main')
        out = m.openai_to_anthropic('m', self._resp())
        self.assertEqual(out.get('stop_reason'), 'tool_use')

    def test_shared_stream_finish_branch_upgrades(self):
        from common.translations.anthropic_stream import AnthropicStreamState
        st = AnthropicStreamState('m')
        frags = ['Checking. ', '<|DS', 'ML|tool_calls>\n<|DSML|invoke name="get_weather">\n',
                 '<|DSML|parameter name="city" string="true">Jakarta</|DSML|parameter>\n',
                 '</|DSML|invoke>\n</|DSML|tool_calls>', ' Done.']
        evts = list(st.start_events())
        chunks = _chat_sse_bytes(frags)
        for blk in chunks:
            payload = blk.decode().split('data: ', 1)[1].strip()
            if payload == '[DONE]':
                continue
            evts.extend(st.translate_chunk(json.loads(payload)))
        joined = '\n'.join(evts)
        self.assertIn('"stop_reason": "tool_use"', joined)
        self.assertIn('get_weather', joined)
        text_deltas = re.findall(r'"type": "text_delta", "text": "((?:[^"\\]|\\.)*)"', joined)
        visible = ''.join(t.encode().decode('unicode_escape') for t in text_deltas)
        self.assertNotIn('DSML', visible)
        self.assertNotIn('invoke', visible)


class TestR5DsmlEnvIndependence(unittest.TestCase):
    """DSML tool markup is protocol breakage, not cosmetic token noise:
    suppression must stay active even with SPECIAL_TOKEN_FILTER=0 (the env
    knob disables only token cosmetics)."""

    def test_chunk_scrub_dsml_not_gated_by_token_filter_env(self):
        import os
        os.environ['SPECIAL_TOKEN_FILTER'] = '0'
        _st_reset_caches()
        try:
            dsml = DsmlMarkupFilter()
            ft, fr = SpecialTokenFilter(), SpecialTokenFilter()
            obj = {'choices': [{'index': 0, 'delta': {'content': '<|DSML|tool_calls>\n<|DSML|invoke name="f">'}, 'finish_reason': None}]}
            scrub_chat_chunk_inplace(obj, ft, fr, dsml=dsml)
            self.assertEqual(obj['choices'][0]['delta']['content'], '')
        finally:
            os.environ.pop('SPECIAL_TOKEN_FILTER', None)
            _st_reset_caches()

    def test_body_scrub_dsml_not_gated_by_token_filter_env(self):
        import os
        os.environ['SPECIAL_TOKEN_FILTER'] = '0'
        _st_reset_caches()
        try:
            body = {'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant',
                                             'content': 'Hi. <|DSML|tool_calls>\n<|DSML|invoke name="f">\nTRUNCATED'}}]}
            scrub_openai_response_inplace(body)
            visible = body['choices'][0]['message']['content']
            self.assertNotIn('DSML', visible)
            self.assertNotIn('invoke', visible)
            self.assertIn('Hi.', visible)
        finally:
            os.environ.pop('SPECIAL_TOKEN_FILTER', None)
            _st_reset_caches()


if __name__ == '__main__':
    unittest.main(verbosity=2)
