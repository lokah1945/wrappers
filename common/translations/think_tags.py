"""Think-tag stream hygiene shared by all wrappers (anti-trace contract).

Empirical root causes this module eliminates (2026-10-15 forensics):
1. ``reasoning_content`` deltas were marker-parsed: a literal closing tag
   quoted inside the model's own reasoning split the thinking block and
   leaked the rest of the reasoning into visible prose.
2. Tag search ran on ``find`` over whole chunks: literal ``<thinking>``
   mentions mid-prose falsely triggered extraction, producing mid-sentence
   thinking fragments.
3. Once ``completed_thinking`` latched, later content chunks bypassed all
   marker handling: the trailing ``</think>`` emitted by NIM at the end of a
   nemotron turn leaked into the text block (transcript tails:
   ``'...start it.</think>\\n'``).
4. Tag markers split across stream chunk boundaries never matched.
5. Whitespace-only content deltas (NIM emits ``'\\n'`` after tool_calls)
   opened standalone text blocks rendered as junk lines.

Design:
- reasoning_content is upstream-extracted reasoning: NEVER marker-parsed.
- Inline thinking is extracted ONLY when the content stream begins with an
  opener tag (start-anchored). Mid-prose tag mentions stay literal text.
- All markers match across chunk boundaries via a small hold-back buffer.
- Trailing stray closers before end-of-message or tool boundaries are
  stripped; clients never render protocol control tokens as prose.
"""
from __future__ import annotations

import re

THINK_OPENERS = ('<think>', '<thinking>')
THINK_CLOSERS = ('</think>', '</thinking>')
_PAIRS = (('<think>', '</think>'), ('<thinking>', '</thinking>'))
_MAX_TAG = max(len(t) for t in THINK_CLOSERS + THINK_OPENERS)
_TRUTHY = ('thinking', 'text')


def _longest_closer_prefix_tail(buf: str) -> int:
    """Length of the longest suffix of buf that is a proper prefix of a closer.

    The suffix must be non-empty and shorter than a full closer, OR a full
    closer sitting at the very end (held as a trailing-stray candidate).
    """
    best = 0
    limit = min(len(buf), _MAX_TAG)
    for size in range(1, limit + 1):
        tail = buf[-size:]
        for closer in THINK_CLOSERS:
            if tail == closer or closer.startswith(tail):
                best = size
                break
    return best


def _find_closer(buf: str):
    """Earliest (index, length) of a full closing tag in buf, or (None, 0)."""
    best_idx, best_len = None, 0
    for closer in THINK_CLOSERS:
        idx = buf.find(closer)
        if idx != -1 and (best_idx is None or idx < best_idx):
            best_idx, best_len = idx, len(closer)
    return best_idx, best_len


def strip_trailing_think_tags(text: str) -> str:
    """Remove stray trailing closer tags (plus trailing whitespace).

    Evidence: NIM nemotron turns end their content with a literal closing
    tag artifact; keeping it left ``</think>\\n`` visible in Claude Code.
    Mid-text tags are preserved (legitimate prose about the protocol).
    """
    if not isinstance(text, str) or not text:
        return text or ''
    out = text.rstrip()
    while True:
        for closer in THINK_CLOSERS:
            if out.endswith(closer):
                out = out[:-len(closer)].rstrip()
                break
        else:
            break
    return out


def extract_inline_thinking(content) -> tuple[str, str]:
    """Start-anchored inline-thinking extraction for non-stream content.

    Returns (thinking, visible_text). Only content that begins with an
    opener tag is treated as inline reasoning; the extracted span is removed
    from the visible text and trailing stray closers are stripped.
    """
    if not isinstance(content, str) or not content:
        return '', content if isinstance(content, str) else ''
    thinking = ''
    text = content
    stripped = text.lstrip()
    for opener, closer in _PAIRS:
        if stripped.startswith(opener):
            rest = stripped[len(opener):]
            end = rest.find(closer)
            if end != -1:
                thinking = rest[:end].strip()
                text = rest[end + len(closer):]
            else:
                thinking = rest.strip()
                text = ''
            break
    return thinking, strip_trailing_think_tags(text)


class ThinkTagStreamFilter:
    """Stateful content-stream filter implementing the anti-trace contract.

    Usage:
        filt = ThinkTagStreamFilter()
        for chunk in content_chunks:
            for kind, piece in filt.feed(chunk):
                emit(kind, piece)            # kind in ('thinking', 'text')
        # before opening a tool block / at stream end:
        for kind, piece in filt.flush_tool_boundary():
            emit(kind, piece)
        # at end of stream:
        for kind, piece in filt.close():
            emit(kind, piece)
    """

    def __init__(self):
        self._mode = 'detect'   # detect -> thinking -> text
        self._buf = ''

    @property
    def in_thinking(self) -> bool:
        return self._mode == 'thinking'

    # ---- internals -----------------------------------------------------
    def _emit_safe_text(self):
        """Release buffered text, holding back possible closer tails."""
        out = []
        while True:
            idx, clen = _find_closer(self._buf)
            if idx is not None:
                tail = self._buf[idx:]
                if _STRAY_TAIL_RE.match(tail):
                    # From this closer to the end of the buffer there is
                    # nothing but closers and whitespace: a trailing-stray
                    # candidate region. Hold it all; if real text follows
                    # later it will be released as a literal mention.
                    out.append(('text', self._buf[:idx]))
                    self._buf = self._buf[idx:]
                else:
                    # genuine mid-text mention: release tag + continue
                    out.append(('text', self._buf[:idx + clen]))
                    self._buf = self._buf[idx + clen:]
                    continue
                break
            keep = _longest_closer_prefix_tail(self._buf)
            if keep and keep < len(self._buf):
                out.append(('text', self._buf[:-keep]))
                self._buf = self._buf[-keep:]
            elif keep and keep == len(self._buf):
                # entire buffer is a partial/full closer: hold everything
                pass
            else:
                if self._buf:
                    out.append(('text', self._buf))
                self._buf = ''
            break
        return out

    def _emit_safe_thinking(self):
        """Release buffered thinking, holding back a possible split closer."""
        out = []
        while True:
            idx, clen = _find_closer(self._buf)
            if idx is not None:
                if self._buf[:idx]:
                    out.append(('thinking', self._buf[:idx]))
                self._buf = self._buf[idx + clen:]
                self._mode = 'text'
                # remaining buffer re-processed as text
                out.extend(self._emit_safe_text())
                break
            keep = _longest_closer_prefix_tail(self._buf)
            if keep and keep < len(self._buf):
                out.append(('thinking', self._buf[:-keep]))
                self._buf = self._buf[-keep:]
            elif not keep:
                if self._buf:
                    out.append(('thinking', self._buf))
                self._buf = ''
            # keep == len(buf): partial closer; hold
            break
        return out

    # ---- public API ----------------------------------------------------
    def feed(self, chunk: str):
        if not isinstance(chunk, str) or not chunk:
            return []
        self._buf += chunk
        if self._mode == 'detect':
            stripped = self._buf.lstrip()
            for opener in THINK_OPENERS:
                if stripped.startswith(opener):
                    self._mode = 'thinking'
                    self._buf = stripped[len(opener):]
                    return self._emit_safe_thinking()
            # not (yet) an opener: is it still a possible prefix?
            undecided = bool(stripped) and any(
                opener.startswith(stripped) for opener in THINK_OPENERS)
            if undecided or not stripped:
                return []          # hold until resolved
            self._mode = 'text'
            return self._emit_safe_text()
        if self._mode == 'thinking':
            return self._emit_safe_thinking()
        return self._emit_safe_text()

    def _flush_boundary(self, *, strip_tags: bool):
        out = []
        if self._mode == 'detect':
            # nothing meaningful seen; drop pure-whitespace holdback
            self._buf = ''
            return out
        if strip_tags:
            if self._mode == 'thinking':
                # never closed: everything buffered is thinking
                if self._buf:
                    out.append(('thinking', self._buf))
                self._buf = ''
                return out
            cleaned = strip_trailing_think_tags(self._buf)
            self._buf = ''
            if cleaned:
                out.append(('text', cleaned))
            return out
        # non-stripping flush (unused today, kept for completeness)
        if self._buf:
            out.append(('text', self._buf))
        self._buf = ''
        return out

    def flush_tool_boundary(self):
        """Flush pending text before a tool block opens; stray tags removed."""
        return self._flush_boundary(strip_tags=True)

    def close(self):
        """Final flush at end of stream; trailing stray closers removed."""
        return self._flush_boundary(strip_tags=True)


_THINK_TAG_RE = re.compile(
    r'(?:<think>|<thinking>|</think>|</thinking>)')


_STRAY_TAIL_RE = re.compile(
    r'^(?:\s|' + '|'.join(re.escape(c) for c in THINK_CLOSERS) + r')*$')


def contains_think_tags(text) -> bool:
    if not isinstance(text, str):
        return False
    return bool(_THINK_TAG_RE.search(text))
