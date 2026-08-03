#!/usr/bin/env python3
"""Central special-token scrubbing for model-visible text.

Root cause (audit P0-4, user report `"><unk><unk><unk>…"`):
byte-level-BPE upstream models (Nemotron, DeepSeek-distill, Qwen, Kimi) emit
tokenizer control tokens in content and reasoning streams: ``<unk>`` (often
**fragmented across chunks** — ``<un`` then ``k>``), ``<s>``, ``</s>``,
``<|im_start|>``, ``<|im_end|>``, ``<|endoftext|>``, fullwidth-pipe DeepSeek
forms (``<｜end▁of▁sentence｜>``), BERT forms (``[UNK]`` …) and the U+0800
Samaritan-letter detokenization artifact. No layer of the proxy filtered
them, so they arrived verbatim in Claude Code / Codex output and were then
persisted into conversation history, poisoning later turns.

This module provides:

  ``filter_special_tokens(text)``        one-shot scrub (non-stream bodies)
  ``SpecialTokenFilter``                  stateful scrubber for SSE deltas —
                                          holds back a short tail so a token
                                          split across chunk boundaries is
                                          still caught; ``flush()`` at end.

Env knobs:
  ``SPECIAL_TOKEN_FILTER``        '0'/'false'/'off' disables scrubbing entirely.
  ``SPECIAL_TOKEN_FILTER_GENERIC`` '0' disables the generic ``<|…|>`` pattern
                                  (keep only the explicit list).
  ``SPECIAL_TOKEN_FILTER_EXTRA``  comma-separated additional literals.

A module-level counter of removed occurrences is exposed for metrics via
``filtered_count()``.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

# ── token inventory ───────────────────────────────────────────────────────
# Explicit literals. Longest first so no shorter pattern eats a longer
# token's prefix. Anything tokenizer-specific that is vanishingly unlikely
# to be legitimate assistant prose.
_EXPLICIT_TOKENS: List[str] = [
    # OpenAI / Llama / ChatML / Qwen / DeepSeek(ascii) angle forms
    '<|endoftext|>', '<|im_start|>', '<|im_end|>', '<|im_sep|>',
    '<|start_header_id|>', '<|end_header_id|>', '<|eot_id|>',
    '<|begin_of_text|>', '<|end_of_text|>', '<|start_of_text|>',
    '<|fim_prefix|>', '<|fim_middle|>', '<|fim_suffix|>', '<|fim_pad|>',
    '<|repo_name|>', '<|file_sep|>', '<|finetune_right_pad_id|>',
    '<|eot|>', '<|end|>', '<|start|>', '<|channel|>', '<|message|>',
    '<|assistant|>', '<|user|>', '<|system|>', '<|tool|>',
    '<|padding|>', '<|freezing|>', '<|dummy_token|>', '<|reflection|>',
    # Classic control tokens
    '<s>', '</s>', '<unk>', '<UNK>', '<pad>', '<PAD>', '<mask>', '<MASK>',
    '<sep>', '<SEP>', '<cls>', '<CLS>', '<bos>', '<BOS>', '<eos>', '<EOS>',
    # BERT bracket forms
    '[UNK]', '[PAD]', '[MASK]', '[CLS]', '[SEP]', '[BOS]', '[EOS]',
    # Detokenization artifacts (byte-level BPE invalid-byte fallbacks)
    '\u0800',  # SAMARITAN LETTER ALAF — appears as 'ࠀ' glued to <unk> runs
]

# Generic angle-pipe patterns: covers every current and future <|…|> /
# <｜…｜> special (ChatML, Harmony, DeepSeek, Llama-3, Mistral, Qwen).
# Bounded inner length keeps pathological regex backtracking away; newlines
# are excluded because control tokens never contain them.
_GENERIC_RE = r'<[|｜][^|｜<>\n]{0,60}[|｜]>'

_PATTERN: Optional[re.Pattern] = None
_ENABLED_CACHE: Optional[bool] = None

_COUNT = {'filtered': 0}

# Max chars that may be withheld while streaming looking for a token tail.
_HOLD_WINDOW = 96


def _build_pattern() -> re.Pattern:
    global _PATTERN
    if _PATTERN is not None:
        return _PATTERN
    generic_on = (os.environ.get('SPECIAL_TOKEN_FILTER_GENERIC') or '1').strip().lower() \
        not in ('0', 'false', 'no', 'off')
    extra = [t.strip() for t in (os.environ.get('SPECIAL_TOKEN_FILTER_EXTRA') or '').split(',') if t.strip()]
    parts = [re.escape(t) for t in sorted(set([*_EXPLICIT_TOKENS, *extra]), key=len, reverse=True)]
    if generic_on:
        parts.insert(0, _GENERIC_RE)
    _PATTERN = re.compile('|'.join(parts))
    return _PATTERN


def filter_enabled() -> bool:
    global _ENABLED_CACHE
    if _ENABLED_CACHE is None:
        _ENABLED_CACHE = (os.environ.get('SPECIAL_TOKEN_FILTER') or '1').strip().lower() \
            not in ('0', 'false', 'no', 'off', 'n')
    return _ENABLED_CACHE


def reset_caches() -> None:
    """Tests: re-read env configuration."""
    global _PATTERN, _ENABLED_CACHE
    _PATTERN = None
    _ENABLED_CACHE = None


def filtered_count() -> int:
    return _COUNT['filtered']


def filter_special_tokens(text: str) -> str:
    """One-shot scrub of a complete text (non-stream paths)."""
    if not text or not filter_enabled():
        return text
    out, n = _build_pattern().subn('', text)
    if n:
        _COUNT['filtered'] += n
    return out


# Candidate token start glyphs — a trailing occurrence of any of these inside
# the hold window might be the beginning of a not-yet-complete token.
_START_GLYPHS = ('<', '|', '\uff5c')


class SpecialTokenFilter:
    """Stateful streaming scrubber.

    ``feed(delta)`` returns text safe to emit downstream immediately; bytes
    that could still grow into a special token are withheld (at most
    ``_HOLD_WINDOW`` chars). ``flush()`` returns and clears whatever remains
    (call exactly once when the stream terminates).

    One filter per text channel (e.g. one for content, one for reasoning):
    flushing emits into the same channel it was fed from.
    """

    __slots__ = ('_buf',)

    def __init__(self) -> None:
        self._buf = ''

    def _holdback_start(self, s: str) -> int:
        """Earliest index from which the tail could still become a token."""
        start = len(s)
        window_start = max(0, len(s) - _HOLD_WINDOW)
        for g in _START_GLYPHS:
            idx = s.rfind(g, window_start)
            if idx != -1:
                start = min(start, idx)
        return start

    def feed(self, text: str) -> str:
        if not text or not filter_enabled():
            return text
        self._buf += text
        scrubbed = filter_special_tokens(self._buf)
        cut = self._holdback_start(scrubbed)
        emit, self._buf = scrubbed[:cut], scrubbed[cut:]
        return emit

    def flush(self) -> str:
        if not self._buf:
            return ''
        out = filter_special_tokens(self._buf)
        self._buf = ''
        return out


def new_filter() -> SpecialTokenFilter:
    return SpecialTokenFilter()


# ── DSML tool-markup suppression (stream) ─────────────────────────────────
# MiniMax models can leak their internal tool protocol
#   <|DSML|tool_calls>…</|DSML|tool_calls>   (also fullwidth <｜DSML｜…>)
# into the visible content stream, fragmented across chunks. The old
# per-chunk `if 'DSML' in chunk: drop` check only dropped chunks that
# happened to contain the literal 'DSML' and leaked everything else
# (parameter values, closing tags) — visible protocol garbage in
# Claude Code / Codex output, and broken turn parsing ("respon berhenti").
#
# DsmlMarkupFilter is a STATEFUL suppressor: feed() returns only text that
# is guaranteed to be outside DSML markup; markup segments are collected
# (collected_text) so callers may optionally recover structured tool calls
# via common.translations.parse_dsml_from_text() at end of stream.
_DSML_OPEN = '<|DSML|'
_DSML_CLOSE = '</|DSML|tool_calls>'
_DSML_PREFIXES = tuple(_DSML_OPEN[:i] for i in range(1, len(_DSML_OPEN)))  # '<','<|','<|D',…
_DSML_COLLECT_CAP = 512 * 1024  # fail-safe vs unterminated-markup memory growth


class DsmlMarkupFilter:
    """Stream-time DSML markup suppressor (see module note above).

    feed(delta) -> emittable clean text. Detection normalises fullwidth
    ｜ → | for matching only (a 1:1 char map, so indices line up); emitted
    text keeps the original glyphs.
    """

    __slots__ = ('_buf', '_collecting', '_collected')

    def __init__(self) -> None:
        self._buf = ''
        self._collecting = False
        self._collected: List[str] = []

    @staticmethod
    def _norm(s: str) -> str:
        return s.replace('｜', '|')

    def feed(self, text: str) -> str:
        if not text:
            return ''
        self._buf += text
        out: List[str] = []
        while True:
            nbuf = self._norm(self._buf)
            if self._collecting:
                j = nbuf.find(_DSML_CLOSE)
                if j == -1:
                    if len(self._buf) > _DSML_COLLECT_CAP:
                        # Fail-safe: unbounded unterminated markup — drop the
                        # collected bytes rather than growing memory forever.
                        self._buf = ''
                        self._collecting = False
                    return ''.join(out)
                self._collected.append(self._buf[:j + len(_DSML_CLOSE)])
                self._buf = self._buf[j + len(_DSML_CLOSE):]
                self._collecting = False
                continue
            i = nbuf.find(_DSML_OPEN)
            if i != -1:
                out.append(self._buf[:i])
                self._buf = self._buf[i:]
                self._collecting = True
                continue
            # Withhold the longest tail that could still become an opener.
            hold = 0
            for p in _DSML_PREFIXES:
                if nbuf.endswith(p):
                    hold = max(hold, len(p))
            if hold:
                out.append(self._buf[:-hold])
                self._buf = self._buf[-hold:]
            else:
                out.append(self._buf)
                self._buf = ''
            return ''.join(out)

    def flush(self) -> str:
        """End-of-stream: emit any clean remainder; drop unterminated markup
        (an incomplete tag must never reach the client)."""
        if self._collecting:
            self._buf = ''
            self._collecting = False
            return ''
        rest, self._buf = self._buf, ''
        return rest

    @property
    def collected_text(self) -> str:
        """All complete DSML markup segments captured so far (for optional
        structured recovery via parse_dsml_from_text)."""
        return ''.join(self._collected)


def new_dsml_filter() -> DsmlMarkupFilter:
    return DsmlMarkupFilter()


def strip_dsml_markup(text: str) -> str:
    """One-shot DSML removal for complete (non-stream) text bodies.

    Removes complete markup segments AND an unterminated trailing segment
    (truncated tool calls must not leak either). Ordinary prose mentioning
    'DSML' — or pipes without the '<|' opener — is untouched.
    """
    if not text or 'DSML' not in str(text).replace('｜', '|'):
        return text
    f = DsmlMarkupFilter()
    return f.feed(text) + f.flush()


# ── OpenAI chat body/chunk scrubbing ─────────────────────────────────────

def scrub_chat_message_inplace(msg: dict) -> None:
    """Scrub visible text fields of a non-stream chat message dict in place."""
    if not isinstance(msg, dict):
        return
    c = msg.get('content')
    if isinstance(c, str):
        # R5 audit: strip MiniMax DSML tool markup from the visible channel
        # on non-stream bodies too — it is the same internal-protocol
        # artifact class as <|im_start|> (P0-4). Tool-call RECOVERY happens
        # on the Anthropic surfaces (parse_dsml_from_text); the plain chat
        # surface only needs the markup gone.
        if 'DSML' in c.replace('｜', '|'):
            _df = DsmlMarkupFilter()
            c = _df.feed(c) + _df.flush()
        msg['content'] = filter_special_tokens(c)
    elif isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                part['text'] = filter_special_tokens(part['text'])
    r = msg.get('reasoning_content')
    if isinstance(r, str):
        msg['reasoning_content'] = filter_special_tokens(r)
    r2 = msg.get('reasoning')
    if isinstance(r2, str):
        msg['reasoning'] = filter_special_tokens(r2)


def scrub_openai_response_inplace(data) -> None:
    """Scrub a full non-stream chat.completion body (choices[].message).

    SPECIAL_TOKEN_FILTER=0 disables only the token scrubbing (cosmetics);
    DSML tool-markup removal stays active — leaked markup breaks client
    parsing, it is not just visual noise."""
    if not isinstance(data, dict):
        return
    choices = data.get('choices')
    if not isinstance(choices, list):
        return
    for ch in choices:
        if isinstance(ch, dict):
            scrub_chat_message_inplace(ch.get('message'))


def scrub_chat_chunk_inplace(obj: dict, filt_text: SpecialTokenFilter,
                             filt_reason: SpecialTokenFilter,
                             dsml: 'DsmlMarkupFilter | None' = None) -> None:
    """Scrub a streaming chat.completion.chunk IN PLACE using stateful
    filters (catches tokens fragmented across chunks). Returns nothing; the
    caller re-serialises ``obj``. Text withheld by the filters is emitted by
    later chunks (or dropped entirely if it was token soup): the stream's
    final flush must call ``flush_into_chunk``.

    R5 audit: pass a ``DsmlMarkupFilter`` as ``dsml`` to also suppress
    MiniMax DSML tool-call markup leaking through the visible content
    channel (cross-chunk safe). DSML suppression is NOT gated on
    SPECIAL_TOKEN_FILTER — markup breakage is a protocol failure, unlike
    cosmetic token noise."""
    if not isinstance(obj, dict):
        return
    choices = obj.get('choices') or []
    if not choices:
        return
    ch0 = choices[0] if isinstance(choices[0], dict) else {}
    delta = ch0.get('delta')
    if not isinstance(delta, dict):
        return
    tok_on = filter_enabled()
    c = delta.get('content')
    if isinstance(c, str) and c:
        if dsml is not None:
            c = dsml.feed(c)
        if c and tok_on:
            delta['content'] = filt_text.feed(c)
        elif not c:
            delta['content'] = ''
        else:
            delta['content'] = c
    r = delta.get('reasoning_content')
    if isinstance(r, str) and r and tok_on:
        delta['reasoning_content'] = filt_reason.feed(r)
    r2 = delta.get('reasoning')
    if isinstance(r2, str) and r2 and tok_on:
        delta['reasoning'] = filt_reason.feed(r2)


def flushed_deltas(filt_text: SpecialTokenFilter, filt_reason: SpecialTokenFilter):
    """Return remaining (content, reasoning) text still withheld — emit as a
    final delta before the terminal chunk of a scrubbed passthrough stream."""
    return filt_text.flush(), filt_reason.flush()


# ── shape-aware passthrough scrubber ─────────────────────────────────────
# Wrappers also forward some upstream SSE streams VERBATIM (native Zen
# /responses, family=messages Anthropic, raw chat/completions). Those bytes
# carried special tokens straight to the user (P0-4) and a synthesized
# terminal frame hid premature EOF (P0-1). This helper re-serialises only
# the frames it understands and tracks whether a natural terminal was seen.

class PassthroughSSE:
    """Line-level scrubber + terminal tracker for verbatim-forwarded SSE.

    Handles the three dialects a wrapper passes through untouched:
      * OpenAI ``chat.completion.chunk``        → shape ``chat``
      * OpenAI Responses events                 → shape ``responses``
      * Anthropic Messages events               → shape ``anthropic``

    ``feed(obj, event_name=None) -> List[dict]`` returns the frames to emit
    (usually the scrubbed object itself; a terminal frame may be preceded by
    a flush frame carrying filter-withheld text). ``flush()`` returns the
    final withheld-text frame(s) for EOF. Unrecognised frames pass through
    untouched so the proxy stays transparent to future event types.

    ``dsml_suppress=False`` leaves MiniMax DSML tool-call markup UNTOUCHED
    in the visible text channel. Use it when a DOWNSTREAM translator
    performs its own DSML suppression + structured tool-call recovery
    (nvidia/openrouter ``/v1/messages``): suppressing here would strip the
    markup bytes before that translator can ever see — and recover — them
    (runtime finding "dsml_stream double-scrub": tool calls silently lost,
    the user's agent just answered prose where a tool call belonged).
    """

    __slots__ = ('_ftext', '_freason', '_dsml_text', 'saw_terminal', 'shape',
                 '_chat_scaffold', '_resp_id', '_resp_cursor',
                 '_anth_idx_text', '_anth_idx_think')

    def __init__(self, dsml_suppress: bool = True) -> None:
        self._ftext = SpecialTokenFilter()
        self._freason = SpecialTokenFilter()
        # R5 audit: also suppress MiniMax DSML tool markup in the visible
        # text channel on every verbatim-forward path (cross-chunk safe).
        self._dsml_text = DsmlMarkupFilter() if dsml_suppress else None
        self.saw_terminal = False  # finish_reason / message_stop / response.completed…
        self.shape: Optional[str] = None
        self._chat_scaffold = {'id': 'chatcmpl-proxy', 'created': 0, 'model': ''}
        self._resp_id: Optional[str] = None
        self._resp_cursor = {'item_id': 'msg-1', 'output_index': 0, 'content_index': 0}
        self._anth_idx_text: Optional[int] = None
        self._anth_idx_think: Optional[int] = None

    # -- internal ------------------------------------------------------
    def _note_chat(self, obj: dict) -> None:
        for k in ('id', 'created', 'model'):
            if obj.get(k):
                self._chat_scaffold[k] = obj[k]

    def feed(self, obj, event_name: Optional[str] = None) -> List[dict]:
        if not isinstance(obj, dict):
            return [obj]
        if obj.get('error') is not None and 'choices' not in obj and 'type' not in obj:
            # upstream error frame — a legitimate terminal signal.
            self.saw_terminal = True
            return [obj]
        t = obj.get('type')
        # ── chat.completion.chunk ──
        if isinstance(obj.get('choices'), list):
            self.shape = self.shape or 'chat'
            self._note_chat(obj)
            ch0 = (obj['choices'] or [None])[0]
            if isinstance(ch0, dict):
                if ch0.get('finish_reason'):
                    self.saw_terminal = True
            scrub_chat_chunk_inplace(obj, self._ftext, self._freason, dsml=self._dsml_text)
            return [obj]
        # ── Responses events ──
        if isinstance(t, str) and t.startswith('response.'):
            self.shape = self.shape or 'responses'
            if t in ('response.completed', 'response.failed', 'response.incomplete'):
                self.saw_terminal = True
            elif t == 'response.created':
                rid = (obj.get('response') or {}).get('id')
                if rid:
                    self._resp_id = rid
            elif t in ('response.output_text.delta', 'response.reasoning_text.delta'):
                for k in ('item_id', 'output_index', 'content_index'):
                    if obj.get(k) is not None:
                        self._resp_cursor[k] = obj[k]
                d = obj.get('delta')
                if isinstance(d, str) and d:
                    f = self._ftext if t == 'response.output_text.delta' else self._freason
                    if t == 'response.output_text.delta' and self._dsml_text is not None:
                        d = self._dsml_text.feed(d)
                    obj['delta'] = f.feed(d) if d else ''
                    if not obj['delta']:
                        return []  # fully-token/markup frame — drop it
            return [obj]
        # ── Anthropic Messages events ──
        if isinstance(t, str) and (t.startswith('message_') or
                                   t.startswith('content_block_') or
                                   t in ('ping', 'error')):
            self.shape = self.shape or 'anthropic'
        if t == 'message_stop':
            self.saw_terminal = True
            return [obj]
        if t == 'message_delta':
            # flush withheld text BEFORE the terminal message_delta.
            out = self._anth_flush_frames()
            out.append(obj)
            self.saw_terminal = True
            return out
        if t == 'error':
            self.saw_terminal = True
            return [obj]
        if t == 'content_block_delta':
            d = obj.get('delta') or {}
            dt = d.get('type')
            if dt == 'text_delta' and isinstance(d.get('text'), str) and d['text']:
                self._anth_idx_text = obj.get('index')
                _txt = d['text']
                if self._dsml_text is not None:
                    _txt = self._dsml_text.feed(_txt)
                d['text'] = self._ftext.feed(_txt)
                if not d['text']:
                    return []
            elif dt == 'thinking_delta' and isinstance(d.get('thinking'), str) and d['thinking']:
                self._anth_idx_think = obj.get('index')
                d['thinking'] = self._freason.feed(d['thinking'])
                if not d['thinking']:
                    return []
            return [obj]
        if t == 'content_block_stop':
            # flush the channel that is closing before the stop frame.
            idx = obj.get('index')
            out: List[dict] = []
            if idx == self._anth_idx_text:
                rest = self._ftext.flush()
                if rest:
                    out.append({'type': 'content_block_delta', 'index': idx,
                                'delta': {'type': 'text_delta', 'text': rest}})
                self._anth_idx_text = None
            if idx == self._anth_idx_think:
                rest = self._freason.flush()
                if rest:
                    out.append({'type': 'content_block_delta', 'index': idx,
                                'delta': {'type': 'thinking_delta', 'thinking': rest}})
                self._anth_idx_think = None
            out.append(obj)
            return out
        # unrecognised → transparent
        return [obj]

    # -- terminal flush --------------------------------------------------
    def _flush_text(self) -> str:
        """Withheld text remainder: flush DSML first (its clean remainder is
        still scrubbed for special tokens), then the token filter."""
        rest = ''
        if self._dsml_text is not None:
            rest = self._ftext.feed(self._dsml_text.flush())
        rest += self._ftext.flush()
        return rest

    def _anth_flush_frames(self) -> List[dict]:
        out: List[dict] = []
        rest_t = self._flush_text()
        if rest_t and self._anth_idx_text is not None:
            out.append({'type': 'content_block_delta', 'index': self._anth_idx_text,
                        'delta': {'type': 'text_delta', 'text': rest_t}})
        rest_r = self._freason.flush()
        if rest_r and self._anth_idx_think is not None:
            out.append({'type': 'content_block_delta', 'index': self._anth_idx_think,
                        'delta': {'type': 'thinking_delta', 'thinking': rest_r}})
        return out

    def flush(self) -> List[dict]:
        """Withheld-text frame(s) emitted at EOF, shape-appropriate."""
        ft = self._flush_text()
        fr = self._freason.flush()
        if not ft and not fr:
            return []
        out: List[dict] = []
        if self.shape == 'responses':
            if ft:
                out.append({'type': 'response.output_text.delta', **self._resp_cursor, 'delta': ft})
            if fr:
                out.append({'type': 'response.reasoning_text.delta', **self._resp_cursor, 'delta': fr})
        elif self.shape == 'anthropic':
            if ft and self._anth_idx_text is not None:
                out.append({'type': 'content_block_delta', 'index': self._anth_idx_text,
                            'delta': {'type': 'text_delta', 'text': ft}})
            if fr and self._anth_idx_think is not None:
                out.append({'type': 'content_block_delta', 'index': self._anth_idx_think,
                            'delta': {'type': 'thinking_delta', 'thinking': fr}})
        else:
            d = {}
            if ft:
                d['content'] = ft
            if fr:
                d['reasoning_content'] = fr
            out.append({'id': self._chat_scaffold['id'], 'object': 'chat.completion.chunk',
                        'created': self._chat_scaffold['created'] or 0,
                        'model': self._chat_scaffold['model'],
                        'choices': [{'index': 0, 'delta': d, 'finish_reason': None}]})
        return out

    def premature_eof_frame(self, message: str) -> dict:
        """P0-1: error frame to emit shape-appropriately when the upstream
        closed without any terminal signal (WRAPPER_CONTRACT §3.3)."""
        if self.shape == 'responses':
            return {'type': 'response.failed',
                    'response': {'id': self._resp_id or 'resp_unknown', 'object': 'response',
                                 'status': 'failed',
                                 'error': {'code': 'upstream_premature_eof', 'message': message}}}
        if self.shape == 'anthropic':
            return {'type': 'error', 'error': {'type': 'api_error', 'message': message}}
        return {'error': {'type': 'api_error', 'code': 'upstream_premature_eof', 'message': message}}

    @staticmethod
    def event_name_for(obj) -> Optional[str]:
        """SSE event: name for a re-serialised frame (Anthropic/Responses
        dialects carry one; chat chunks are data-only)."""
        if isinstance(obj, dict):
            t = obj.get('type')
            if isinstance(t, str) and (t.startswith('response.') or t in (
                    'message_start', 'message_delta', 'message_stop', 'content_block_start',
                    'content_block_delta', 'content_block_stop', 'error', 'ping')):
                return t
        return None


# ── byte-level passthrough driver (shared, CONTRACT §7) ──────────────────
# Every wrapper's verbatim SSE forward path used to keep its own copy of the
# block parser below (opencode _emit_block, blackbox _emit_block, openrouter
# _emit_block, nvidia byte loop). One forked behaviour = one place bugs live
# (P0-1 premature EOF, P0-4 token leakage). The single canonical
# implementation now lives here; wrappers drive it and keep only their
# pool/release semantics.

PREMATURE_EOF_MSG = (
    "upstream stream ended prematurely: EOF without a terminal signal "
    "(finish_reason/message_stop/response.completed); the response may be "
    "truncated — client may retry"
)

DONE_WITHOUT_FINISH_MSG = (
    "upstream stream ended with [DONE] but no finish_reason — the response "
    "may be truncated — client may retry"
)


def sse_block(obj, event_name: Optional[str] = None) -> bytes:
    """Serialise one SSE event block (event: line only when given)."""
    import json as _json
    head = f"event: {event_name}\n" if event_name else ""
    return (head + "data: " + _json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


class PassthroughBlockRewriter:
    """Byte-level driver for verbatim-forwarded SSE streams.

    Responsibilities (single shared implementation, CONTRACT §7):
      * CRLF → LF parity (nous N-08 class bug),
      * ``\\n\\n`` block buffering across arbitrary chunk splits,
      * re-serialisation through :class:`PassthroughSSE` so special tokens
        are scrubbed even on the pass-through path (P0-4, ``\\"><unk>…"``),
      * ``data: [DONE]`` / terminal-signal tracking,
      * premature-EOF → shape-appropriate ERROR frame instead of a
        fabricated success terminator (P0-1, CONTRACT §3.3).

    Usage::

        rw = PassthroughBlockRewriter()      # dsml_suppress=False per surface
        async for idle, chunk in chunk_stream(resp):
            if idle:
                if rw.at_block_boundary():
                    yield b": heartbeat\\n\\n"
                continue
            for frame in rw.feed(chunk):
                yield frame
        for frame in rw.finish(terminal_done=True):
            yield frame

    ``terminal_done=True`` appends ``data: [DONE]`` when the upstream never
    sent one (OpenAI-dialect surfaces). Anthropic native pass-through must
    use ``terminal_done=False`` (its terminator is ``message_stop``).
    """

    __slots__ = ('scrub', 'buf', 'saw_done', '_premature_emitted')

    def __init__(self, scrub: Optional[PassthroughSSE] = None,
                 dsml_suppress: bool = True) -> None:
        # dsml_suppress=False (see PassthroughSSE): leave DSML markup bytes
        # intact for a downstream recovering translator (/v1/messages).
        self.scrub = scrub or PassthroughSSE(dsml_suppress=dsml_suppress)
        self.buf = b""
        self.saw_done = False
        self._premature_emitted = False

    # -- block parsing ------------------------------------------------
    def _emit_block(self, block: bytes, is_tail: bool = False):
        """Parse one SSE event block -> (frames_bytes, is_done_terminator).

        ``is_tail=True`` marks the final partial block at EOF: a truncated,
        unparseable tail is CORRUPTION (it would break the client's SSE
        parser) and is dropped — the premature-EOF error frame follows via
        ``finish()``. Mid-stream unparsable blocks still pass through
        verbatim so the proxy stays transparent to future event types."""
        import json as _json
        event_name = None
        data_lines = []
        for raw in block.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith(b"event:"):
                event_name = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        out = []
        if not data_lines:
            # comment / heartbeat / unknown block — forward verbatim.
            if block.strip():
                out.append(block + (b"" if block.endswith(b"\n\n") else b"\n\n"))
            return out, False
        payload = b"\n".join(data_lines)
        if payload in (b"[DONE]", b'"[DONE]"'):
            # P0-4: release filter-withheld text BEFORE the terminator.
            for f in self.scrub.flush():
                out.append(sse_block(f, PassthroughSSE.event_name_for(f)))
            # P0-1 parity (CONTRACT §3.3): a healthy OpenAI stream always
            # sends finish_reason before [DONE], so a bare [DONE] is a
            # truncation signal from a middlebox — surface an error frame
            # BEFORE the terminator instead of masquerading success.
            if not self.scrub.saw_terminal and not self._premature_emitted:
                self._premature_emitted = True
                ef = self.scrub.premature_eof_frame(DONE_WITHOUT_FINISH_MSG)
                out.append(sse_block(ef, PassthroughSSE.event_name_for(ef)))
            out.append(b"data: [DONE]\n\n")
            return out, True
        try:
            obj = _json.loads(payload)
        except ValueError:
            if is_tail:
                # Truncated final frame (e.g. TCP cut mid-JSON): forwarding it
                # would make strict SDK SSE parsers fail at the very end of an
                # otherwise salvageable stream. Drop + let finish() surface
                # the error instead.
                try:
                    import logging as _logging
                    _logging.getLogger('wrapper-sanitize').warning(
                        '[passthrough] dropping truncated tail frame at EOF (%dB): %r',
                        len(payload), payload[:160])
                except Exception:
                    pass
                return [], False
            # Unparsable — forward the original block untouched so the proxy
            # stays transparent to future/foreign event types.
            return [block + (b"" if block.endswith(b"\n\n") else b"\n\n")], False
        for f in self.scrub.feed(obj, event_name):
            name = event_name or PassthroughSSE.event_name_for(f)
            out.append(sse_block(f, name))
        return out, False

    # -- public -------------------------------------------------------
    def feed(self, chunk) -> List[bytes]:
        """Feed one upstream chunk; returns SSE frames ready to forward."""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        self.buf += bytes(chunk)
        if b"\r" in self.buf:  # CRLF parity
            self.buf = self.buf.replace(b"\r\n", b"\n")
        out: List[bytes] = []
        while b"\n\n" in self.buf:
            block, self.buf = self.buf.split(b"\n\n", 1)
            frames, is_done = self._emit_block(block)
            out.extend(frames)
            if is_done:
                self.saw_done = True
        return out

    def at_block_boundary(self) -> bool:
        """True when no partial SSE block is buffered — safe point to inject
        a heartbeat comment without splitting a data: line (OC-5)."""
        return not self.buf

    def finish(self, terminal_done: bool = True,
               premature_msg: str = PREMATURE_EOF_MSG) -> List[bytes]:
        """EOF: flush the tail + withheld text, surface premature EOF as an
        error frame, and (optionally) append the dialect terminator."""
        out: List[bytes] = []
        tail = self.buf.strip()
        self.buf = b""
        if tail:
            frames, is_done = self._emit_block(tail, is_tail=True)
            out.extend(frames)
            if is_done:
                self.saw_done = True
        # P0-4: release any text still withheld by the token filter.
        for f in self.scrub.flush():
            out.append(sse_block(f, PassthroughSSE.event_name_for(f)))
        # P0-1: upstream closed without ANY terminal signal → error, not a
        # fabricated success (CONTRACT §3.3). Guard against double-emit when
        # a bare [DONE] already carried the error frame.
        if not self.scrub.saw_terminal and not self._premature_emitted:
            self._premature_emitted = True
            ef = self.scrub.premature_eof_frame(premature_msg)
            out.append(sse_block(ef, PassthroughSSE.event_name_for(ef)))
        if terminal_done and not self.saw_done:
            out.append(b"data: [DONE]\n\n")
        return out
