#!/usr/bin/env python3
"""Patch nous/src/main.py: anti-trace think-tag hygiene (stream + non-stream).

- AnthropicStreamState gains a ThinkTagStreamFilter for content deltas
  (reasoning_content stays direct, never marker-parsed).
- Whitespace-only content pieces never open a standalone text block.
- Tool-block boundaries and finish/done paths flush the filter with stray
  trailing think-tags stripped.
- openai_to_anthropic (non-stream): start-anchored inline extraction +
  trailing stray cleanup.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/nous/src/main.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if 'def _emit_thinking_block' in text and 'def _emit_text_block' in text:
        die('already fully patched')

    if '_ThinkTagStreamFilter' not in text:
        backup = SRC.with_suffix('.py.bak-' + STAMP + '-thinktrace')
        shutil.copy2(SRC, backup)
        print(f'backup: {backup}')
    else:
        print('resuming partially patched file (no new backup)')

    def replace_once(old, new, what):
        nonlocal text
        count = text.count(old)
        if count == 0:
            if new and new in text:
                print(f'skip (already applied): {what}')
                return
            die(f'anchor for {what} found 0 times (need exactly 1)')
        if count != 1:
            die(f'anchor for {what} found {count} times (need exactly 1)')
        text = text.replace(old, new)
        print(f'patched: {what}')

    # ---- 1. import ----
    replace_once(
        "        openai_chat_to_anthropic_request,\n"
        "    )\n"
        "    from common.compat import (\n",
        "        openai_chat_to_anthropic_request,\n"
        "    )\n"
        "    from common.translations.think_tags import (\n"
        "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
        "        extract_inline_thinking as _extract_inline_thinking,\n"
        "        strip_trailing_think_tags as _strip_think_tags,\n"
        "    )\n"
        "    from common.compat import (\n",
        'import')

    # ---- 2. non-stream openai_to_anthropic: inline extraction ----
    replace_once(
        '    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""\n'
        '\n'
        '    content = []\n'
        '    # Keep thinking SEPARATE — never concatenate into text (Claude Code contract)\n',
        '    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""\n'
        '\n'
        '    # Anti-trace (2026-10-15 forensics): start-anchored inline thinking\n'
        '    # extraction + trailing stray think-tag cleanup. Nous reasoning\n'
        '    # models can emit inline reasoning tags in content; the extracted\n'
        '    # span is removed from visible text, literal mid-prose mentions are\n'
        '    # preserved, and stray trailing closers never render as prose.\n'
        '    if isinstance(text, str):\n'
        '        if not reasoning:\n'
        '            _inline, text = _extract_inline_thinking(text)\n'
        '            if _inline:\n'
        '                reasoning = _inline\n'
        '        else:\n'
        '            text = _strip_think_tags(text)\n'
        '\n'
        '    content = []\n'
        '    # Keep thinking SEPARATE — never concatenate into text (Claude Code contract)\n',
        'non-stream-inline')

    # ---- 3. stream state init ----
    replace_once(
        '        self.finished = False\n'
        '        self.msg_id = f"msg_{int(time.time()*1000)}"\n',
        '        self.finished = False\n'
        '        self.msg_id = f"msg_{int(time.time()*1000)}"\n'
        '        # Anti-trace: tag-aware content stream filter\n'
        '        self._think_filter = _ThinkTagStreamFilter()\n',
        'state-init')

    # ---- 4. content emission through the filter + helper methods ----
    old_content_block = (
        '        if content:\n'
        '            if self.current_block != "text":\n'
        '                # R-02: close every open tool block before opening text.\n'
        '                for _ti in sorted(self.open_tool_blocks):\n'
        '                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": _ti}})\n'
        '                self.open_tool_blocks.clear()\n'
        '                if self.current_block and self.current_block != "tool_use":\n'
        '                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n'
        '                self.index += 1\n'
        '                events.append({"type": "content_block_start", "data": {\n'
        '                    "type": "content_block_start", "index": self.index,\n'
        '                    "content_block": {"type": "text", "text": ""},\n'
        '                }})\n'
        '                self.current_block = "text"\n'
        '            events.append({"type": "content_block_delta", "data": {\n'
        '                "type": "content_block_delta", "index": self.index,\n'
        '                "delta": {"type": "text_delta", "text": content},\n'
        '            }})\n'
    )
    new_content_block = (
        '        if content:\n'
        '            # Anti-trace: content flows through the shared think-tag\n'
        '            # filter (start-anchored extraction, cross-chunk buffering,\n'
        '            # trailing stray-closer suppression). reasoning_content is\n'
        '            # never marker-parsed.\n'
        '            for _kind, _piece in self._think_filter.feed(content):\n'
        '                if not _piece:\n'
        '                    continue\n'
        '                if _kind == "thinking":\n'
        '                    events.extend(self._emit_thinking_block(_piece))\n'
        '                else:\n'
        '                    # Whitespace-only pieces never open a text block\n'
        '                    # (upstream trailing-newline artifacts).\n'
        '                    if not _piece.strip() and self.current_block != "text":\n'
        '                        continue\n'
        '                    events.extend(self._emit_text_block(_piece))\n'
    )
    replace_once(old_content_block, new_content_block, 'stream-content-filter')

    # ---- 4b. helper methods after _usage (unique anchor) ----
    usage_anchor = (
        '    def _usage(self, raw=None):\n'
        '        raw = raw or {}\n'
        '        return {\n'
        '            "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0,\n'
        '            "output_tokens": raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0,\n'
        '            "cache_creation_input_tokens": raw.get("cache_creation_input_tokens", 0) or 0,\n'
        '            "cache_read_input_tokens": raw.get("cache_read_input_tokens", 0) or 0,\n'
        '        }\n'
    )
    helper_methods = (
        '\n'
        '    def _emit_thinking_block(self, piece):\n'
        '        """Open (if needed) a thinking block and append a thinking_delta."""\n'
        '        events = []\n'
        '        if self.current_block != "thinking":\n'
        '            # R-02: close every open tool block before opening thinking.\n'
        '            for _ti in sorted(self.open_tool_blocks):\n'
        '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": _ti}})\n'
        '            self.open_tool_blocks.clear()\n'
        '            if self.current_block and self.current_block != "tool_use":\n'
        '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n'
        '            self.index += 1\n'
        '            events.append({"type": "content_block_start", "data": {\n'
        '                "type": "content_block_start", "index": self.index,\n'
        '                "content_block": {"type": "thinking", "thinking": ""},\n'
        '            }})\n'
        '            self.current_block = "thinking"\n'
        '        events.append({"type": "content_block_delta", "data": {\n'
        '            "type": "content_block_delta", "index": self.index,\n'
        '            "delta": {"type": "thinking_delta", "thinking": piece},\n'
        '        }})\n'
        '        return events\n'
        '\n'
        '    def _emit_text_block(self, piece):\n'
        '        """Open (if needed) a text block and append a text_delta."""\n'
        '        events = []\n'
        '        if self.current_block != "text":\n'
        '            # R-02: close every open tool block before opening text.\n'
        '            for _ti in sorted(self.open_tool_blocks):\n'
        '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": _ti}})\n'
        '            self.open_tool_blocks.clear()\n'
        '            if self.current_block and self.current_block != "tool_use":\n'
        '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n'
        '            self.index += 1\n'
        '            events.append({"type": "content_block_start", "data": {\n'
        '                "type": "content_block_start", "index": self.index,\n'
        '                "content_block": {"type": "text", "text": ""},\n'
        '            }})\n'
        '            self.current_block = "text"\n'
        '        events.append({"type": "content_block_delta", "data": {\n'
        '            "type": "content_block_delta", "index": self.index,\n'
        '            "delta": {"type": "text_delta", "text": piece},\n'
        '        }})\n'
        '        return events\n'
    )
    replace_once(usage_anchor, usage_anchor + helper_methods, 'helper-methods')

    # ---- 5. tool-boundary flush ----
    replace_once(
        '            if idx not in self.tool_map:\n'
        '                # R-02: close only a text/thinking block here. Closing the\n',
        '            if idx not in self.tool_map:\n'
        '                # Anti-trace: flush pending filtered content (stray\n'
        '                # trailing think-tags stripped) before the tool opens.\n'
        '                for _fkind, _fpiece in self._think_filter.flush_tool_boundary():\n'
        '                    if not _fpiece:\n'
        '                        continue\n'
        '                    if _fkind == "thinking":\n'
        '                        events.extend(self._emit_thinking_block(_fpiece))\n'
        '                    elif _fpiece.strip() or self.current_block == "text":\n'
        '                        events.extend(self._emit_text_block(_fpiece))\n'
        '                # R-02: close only a text/thinking block here. Closing the\n',
        'tool-boundary-flush')

    # ---- 6. finish_reason flush ----
    replace_once(
        '        if ch.get("finish_reason") and not self.finished:\n'
        '            self.finished = True\n'
        '            # R-02: close all open tool blocks, then any text/thinking block.\n',
        '        if ch.get("finish_reason") and not self.finished:\n'
        '            self.finished = True\n'
        '            # Anti-trace: final flush of filtered content; trailing\n'
        '            # stray think-tags never render as visible text.\n'
        '            for _fkind, _fpiece in self._think_filter.close():\n'
        '                if not _fpiece:\n'
        '                    continue\n'
        '                if _fkind == "thinking":\n'
        '                    events.extend(self._emit_thinking_block(_fpiece))\n'
        '                elif _fpiece.strip() or self.current_block == "text":\n'
        '                    events.extend(self._emit_text_block(_fpiece))\n'
        '            # R-02: close all open tool blocks, then any text/thinking block.\n',
        'finish-flush')

    # ---- 7. done() flush (no-finish_reason path) ----
    replace_once(
        '        # B-06: capture before clearing.\n'
        '        was_in_tool_block = (self.current_block == "tool_use") or bool(self.open_tool_blocks)\n',
        '        # Anti-trace: final flush (no-finish_reason path).\n'
        '        for _fkind, _fpiece in self._think_filter.close():\n'
        '            if not _fpiece:\n'
        '                continue\n'
        '            if _fkind == "thinking":\n'
        '                events.extend(self._emit_thinking_block(_fpiece))\n'
        '            elif _fpiece.strip() or self.current_block == "text":\n'
        '                events.extend(self._emit_text_block(_fpiece))\n'
        '        # B-06: capture before clearing.\n'
        '        was_in_tool_block = (self.current_block == "tool_use") or bool(self.open_tool_blocks)\n',
        'done-flush')

    SRC.write_text(text, encoding='utf-8')
    print('written')

    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
