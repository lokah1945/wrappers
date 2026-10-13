"""Model markup decoding for cross-protocol adapters only.

Native payloads never pass through this module. Only a leading think envelope
is structural; tags in prose or Markdown remain literal. Nemotron can omit the
opening token, so its initial content is buffered until a closing token or the
end of the turn. A bounded buffer fails explicitly instead of leaking markup.
"""
from __future__ import annotations

import json
import re
import uuid


class ContentTranslator:
    MAX_BUFFER = 1024 * 1024
    OPEN = ('<think>', '<thinking>')
    CLOSE = ('</think>', '</thinking>')
    DSML_OPEN = ('<|DSML|tool_calls>', '<｜DSML｜tool_calls>',
                 '|DSML|tool_calls>', '｜DSML｜tool_calls>')
    DSML_CLOSE = ('</|DSML|tool_calls>', '</｜DSML｜tool_calls>')

    def __init__(self, model: str = ''):
        self.model = model
        self.mode = 'prefix'
        self.buffer = ''
        self.text_buffer = ''
        self.seen_reasoning = False
        self.implicit = 'nemotron' in model.lower()
        self.code_ticks = 0

    @staticmethod
    def _emit(kind, value):
        return [{'type': kind, 'thinking' if kind == 'thinking' else 'text': value}] if value else []

    @staticmethod
    def _tail(text, tokens):
        return max((n for token in tokens for n in range(1, len(token))
                    if text.endswith(token[:n])), default=0)

    def feed(self, text: str, *, reasoning: bool = False) -> list[dict]:
        if not isinstance(text, str) or not text:
            return []
        if reasoning:
            # A structured reasoning channel remains reasoning even after text.
            self.seen_reasoning = True
            return self._emit('thinking', text)
        self.buffer += text
        if len(self.buffer) > self.MAX_BUFFER:
            raise ValueError('Upstream reasoning delimiter missing; content buffer exceeded 1 MiB')
        return self._drain(False)

    def _drain(self, final: bool) -> list[dict]:
        out = []
        if self.mode == 'prefix':
            stripped = self.buffer.lstrip()
            if not final and (not stripped or any(t.startswith(stripped) for t in (*self.OPEN, *self.CLOSE))):
                return []
            opener = next((t for t in self.OPEN if stripped.startswith(t)), None)
            closer = next((t for t in self.CLOSE if stripped.startswith(t)), None)
            if opener:
                self.buffer = stripped[len(opener):]
                self.mode = 'thinking'
            elif closer and (self.seen_reasoning or self.implicit):
                self.buffer = stripped[len(closer):]
                self.mode = 'text'
            elif self.implicit and not self.seen_reasoning and not stripped.startswith(('`', '~~~')):
                # Omitted opening token is a model-specific convention, not a
                # reason to strip arbitrary closing tags on every model.
                self.mode = 'implicit'
            else:
                self.mode = 'text'
        if self.mode in ('thinking', 'implicit'):
            matches = [(self.buffer.find(t), t) for t in self.CLOSE if t in self.buffer]
            if matches:
                index, closer = min(matches)
                # Quoted/code examples with a closing token are ordinary text.
                if self.mode == 'implicit' and any(x in self.buffer[:index] for x in ('`', '<think>', '<thinking>')):
                    self.mode = 'text'
                else:
                    out += self._emit('thinking', self.buffer[:index])
                    self.seen_reasoning = True
                    self.buffer = self.buffer[index + len(closer):]
                    self.mode = 'text'
            elif self.mode == 'thinking':
                keep = 0 if final else self._tail(self.buffer, self.CLOSE)
                cut = len(self.buffer) - keep
                out += self._emit('thinking', self.buffer[:cut])
                self.buffer = self.buffer[cut:]
                return out
            elif not final:
                return out
            else:
                self.mode = 'text'
        if self.mode == 'text':
            self.text_buffer += self.buffer
            self.buffer = ''
            out += self._drain_text(final)
        return out

    def _drain_text(self, final: bool) -> list[dict]:
        out, literal = [], []
        def flush():
            if literal:
                out.extend(self._emit('text', ''.join(literal)))
                literal.clear()
        while self.text_buffer:
            value = self.text_buffer
            if value[0] == '`':
                run = len(value) - len(value.lstrip('`'))
                if run == len(value) and not final:
                    break
                if not self.code_ticks:
                    self.code_ticks = run
                elif run == self.code_ticks:
                    self.code_ticks = 0
                literal.append(value[:run])
                self.text_buffer = value[run:]
                continue
            if not self.code_ticks:
                opener = next((t for t in self.DSML_OPEN if value.startswith(t)), None)
                if opener:
                    matches = [(value.find(t, len(opener)), t) for t in self.DSML_CLOSE if t in value[len(opener):]]
                    if not matches:
                        if final:
                            raise ValueError('Upstream tool markup ended before its closing delimiter')
                        if len(value) > self.MAX_BUFFER:
                            raise ValueError('Upstream tool markup exceeds 1 MiB')
                        break
                    index, closer = min(matches)
                    flush()
                    segment = value[:index + len(closer)].replace('｜', '|')
                    out.extend(self._tools(segment))
                    self.text_buffer = value[index + len(closer):]
                    continue
                if not final and any(t.startswith(value) for t in self.DSML_OPEN):
                    break
            literal.append(value[0])
            self.text_buffer = value[1:]
        flush()
        return out

    @staticmethod
    def _tools(segment: str) -> list[dict]:
        tools = []
        pattern = r'(?:<)?\|DSML\|invoke\s+name="([^"]+)"[^>]*>(.*?)</\|DSML\|invoke>'
        for match in re.finditer(pattern, segment, re.S):
            name, inner = match.groups()
            params = {}
            for param in re.finditer(r'(?:<)?\|DSML\|parameter\s+name="([^"]+)"([^>]*)>(.*?)</\|DSML\|parameter>', inner, re.S):
                key, attrs, value = param.groups()
                if re.search(r'\bstring="false"', attrs):
                    try:
                        value = json.loads(value)
                    except ValueError as exc:
                        raise ValueError('Invalid JSON parameter in upstream tool markup') from exc
                params[key] = value
            tools.append({'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex,
                          'name': name, 'input': params})
        if not tools:
            raise ValueError('Upstream tool markup contains no complete invocation')
        return tools

    def finish(self) -> list[dict]:
        return self._drain(True)


def translated_content(message: dict, model: str = '') -> list[dict]:
    parser = ContentTranslator(model)
    blocks = parser.feed(message.get('reasoning_content') or message.get('reasoning') or '', reasoning=True)
    content = message.get('content') or ''
    if isinstance(content, list):
        content = ''.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text')
    if not isinstance(content, str):
        raise ValueError('Upstream Chat content must be text or text parts')
    blocks += parser.feed(content) + parser.finish()
    return blocks
