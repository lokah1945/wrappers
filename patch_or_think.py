#!/usr/bin/env python3
"""Patch openrouter/src/main.py: anti-trace think-tag hygiene (stream + non-stream).

- _translate_openai_stream_to_anthropic: content deltas flow through a
  ThinkTagStreamFilter; tool boundaries and the terminal path flush it.
- _openai_to_anthropic_response (non-stream): start-anchored inline
  extraction + trailing stray cleanup.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/openrouter/src/main.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if '_ThinkTagStreamFilter' in text:
        die('already patched')

    backup = SRC.with_suffix('.py.bak-' + STAMP + '-thinktrace')
    shutil.copy2(SRC, backup)
    print(f'backup: {backup}')

    def replace_once(old, new, what):
        nonlocal text
        count = text.count(old)
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

    # ---- 2. non-stream _openai_to_anthropic_response ----
    replace_once(
        '    if message.get("content"):\n'
        '        content_blocks.append({"type": "text", "text": message["content"]})\n',
        '    _or_text = message.get("content")\n'
        '    if isinstance(_or_text, str):\n'
        '        # Anti-trace (2026-10-15 forensics): start-anchored inline\n'
        '        # thinking extraction; extracted span removed from visible\n'
        '        # text; trailing stray closers stripped.\n'
        '        if not reasoning:\n'
        '            _inline, _or_text = _extract_inline_thinking(_or_text)\n'
        '            if _inline:\n'
        '                reasoning = _inline\n'
        '                content_blocks.append({"type": "thinking", "thinking": reasoning})\n'
        '        else:\n'
        '            _or_text = _strip_think_tags(_or_text)\n'
        '    if _or_text:\n'
        '        content_blocks.append({"type": "text", "text": _or_text})\n',
        'non-stream-inline')

    # ---- 3. stream init + helper ----
    replace_once(
        "    tool_call_blocks = {}  # OpenAI tool index → Anthropic block index\n",
        "    tool_call_blocks = {}  # OpenAI tool index → Anthropic block index\n"
        "    thinking_index = 0\n"
        "    _think_filter = _ThinkTagStreamFilter()  # anti-trace: tag-aware content filter\n"
        "\n"
        "    async def _emit_filtered(pieces):\n"
        "        \"\"\"Anti-trace emission: filtered (thinking|text) pieces with\n"
        "        proper block lifecycle (R-02 semantics preserved).\"\"\"\n"
        "        nonlocal block_open, block_index, text_started, thinking_started, thinking_index\n"
        "        for _fkind, _fpiece in pieces:\n"
        "            if not _fpiece:\n"
        "                continue\n"
        "            if _fkind == 'thinking':\n"
        "                if not thinking_started:\n"
        "                    if block_open and text_started:\n"
        "                        yield _sse('content_block_stop', {\n"
        "                            'type': 'content_block_stop', 'index': block_index,\n"
        "                        })\n"
        "                        block_index += 1\n"
        "                        text_started = False\n"
        "                        block_open = False\n"
        "                    thinking_index = block_index\n"
        "                    yield _sse('content_block_start', {\n"
        "                        'type': 'content_block_start', 'index': thinking_index,\n"
        "                        'content_block': {'type': 'thinking', 'thinking': ''},\n"
        "                    })\n"
        "                    thinking_started = True\n"
        "                    block_open = True\n"
        "                    block_index += 1\n"
        "                yield _sse('content_block_delta', {\n"
        "                    'type': 'content_block_delta', 'index': thinking_index,\n"
        "                    'delta': {'type': 'thinking_delta', 'thinking': _fpiece},\n"
        "                })\n"
        "            else:\n"
        "                if not _fpiece.strip() and not text_started:\n"
        "                    continue\n"
        "                if not text_started:\n"
        "                    if block_open and thinking_started:\n"
        "                        yield _sse('content_block_stop', {\n"
        "                            'type': 'content_block_stop', 'index': thinking_index,\n"
        "                        })\n"
        "                        thinking_started = False\n"
        "                        block_open = False\n"
        "                    yield _sse('content_block_start', {\n"
        "                        'type': 'content_block_start', 'index': block_index,\n"
        "                        'content_block': {'type': 'text', 'text': ''},\n"
        "                    })\n"
        "                    text_started = True\n"
        "                    block_open = True\n"
        "                yield _sse('content_block_delta', {\n"
        "                    'type': 'content_block_delta', 'index': block_index,\n"
        "                    'delta': {'type': 'text_delta', 'text': _fpiece},\n"
        "                })\n",
        'stream-init-helper')

    # ---- 4. content emission through the filter ----
    old_content = (
        "                # Text content\n"
        "                if delta.get('content'):\n"
        "                    if not text_started:\n"
        "                        # Close an open thinking block if any (tool blocks stay\n"
        "                        # open concurrently — R-02).\n"
        "                        if block_open and thinking_started:\n"
        "                            yield _sse('content_block_stop', {\n"
        "                                'type': 'content_block_stop', 'index': thinking_index,\n"
        "                            })\n"
        "                            thinking_started = False\n"
        "                            block_open = False\n"
        "                        yield _sse('content_block_start', {\n"
        "                            'type': 'content_block_start', 'index': block_index,\n"
        "                            'content_block': {'type': 'text', 'text': ''},\n"
        "                        })\n"
        "                        text_started = True\n"
        "                        block_open = True\n"
        "                    yield _sse('content_block_delta', {\n"
        "                        'type': 'content_block_delta', 'index': block_index,\n"
        "                        'delta': {'type': 'text_delta', 'text': delta['content']},\n"
        "                    })\n"
    )
    new_content = (
        "                # Text content (anti-trace: think-tag filtered)\n"
        "                if delta.get('content'):\n"
        "                    async for _ev in _emit_filtered(_think_filter.feed(delta['content'])):\n"
        "                        yield _ev\n"
    )
    replace_once(old_content, new_content, 'stream-content-filter')

    # ---- 5. tool-boundary flush ----
    replace_once(
        "                    if tc_idx not in tool_call_blocks:\n"
        "                        # R-02: close only a TEXT or THINKING block here.\n",
        "                    if tc_idx not in tool_call_blocks:\n"
        "                        # Anti-trace: flush pending filtered content\n"
        "                        # (stray trailing think-tags stripped) before\n"
        "                        # the tool block opens.\n"
        "                        async for _ev in _emit_filtered(_think_filter.flush_tool_boundary()):\n"
        "                            yield _ev\n"
        "                        # R-02: close only a TEXT or THINKING block here.\n",
        'tool-boundary-flush')

    # ---- 6. terminal flush ----
    replace_once(
        "        # B-04 / R-02: close the open TEXT/THINKING block (if any), then every\n"
        "        # concurrently-open tool_use block, lowest index first.\n"
        "        if block_open and text_started:\n",
        "        # Anti-trace: final content flush; trailing stray think-tags\n"
        "        # never reach the client as visible text.\n"
        "        async for _ev in _emit_filtered(_think_filter.close()):\n"
        "            yield _ev\n"
        "        # B-04 / R-02: close the open TEXT/THINKING block (if any), then every\n"
        "        # concurrently-open tool_use block, lowest index first.\n"
        "        if block_open and text_started:\n",
        'terminal-flush')

    SRC.write_text(text, encoding='utf-8')
    print('written')

    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
