#!/usr/bin/env python3
"""Patch nvidia-python/src/anthropic_compat.py: anti-trace think-tag hygiene.

Root causes fixed (2026-10-15 forensics, all empirically reproduced):
1. reasoning_content was marker-parsed -> literal '</think>' quoted inside
   reasoning split the thinking block and leaked the rest as prose.
2. Tag search used find() over whole chunks -> literal '<thinking>' mentions
   mid-prose falsely triggered extraction (mid-sentence thinking fragments).
3. completed_thighting latch bypassed all marker handling for later chunks ->
   NIM's trailing '</think>' artifact leaked into visible text.
4. No cross-chunk buffering -> tags split across chunk boundaries never match.
5. Whitespace-only content deltas opened standalone text blocks (junk lines).

Backs up the original first. Idempotence: refuses to run twice.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/nvidia-python/src/anthropic_compat.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if 'ThinkTagStreamFilter' in text:
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
        "from .capabilities import get_context_window\n",
        "from .capabilities import get_context_window\n"
        "from common.translations.think_tags import ThinkTagStreamFilter, strip_trailing_think_tags\n",
        'import')

    # ---- 2. stream state init ----
    replace_once(
        "    client_gone = False  # V-15 fix: set on GeneratorExit/CancelledError\n",
        "    client_gone = False  # V-15 fix: set on GeneratorExit/CancelledError\n"
        "    _think_filter = ThinkTagStreamFilter()  # anti-trace: tag-aware content filter\n",
        'state-init')

    # ---- 3. replace parse_and_emit entirely ----
    start_marker = "    async def parse_and_emit(chunk, is_reasoning):\n"
    end_marker = "    yield _sse('message_start', {\n"
    start = text.find(start_marker)
    if start == -1:
        die('parse_and_emit start not found')
    end = text.find(end_marker, start)
    if end == -1 or end - start > 20000:
        die('message_start anchor not found after parse_and_emit')

    new_block = '''    async def _emit_filtered_content(text):
        """Anti-trace content emission: think-tag aware, whitespace guarded."""
        for _kind, _piece in _think_filter.feed(text):
            if not _piece:
                continue
            if _kind == 'thinking':
                async for c in emit_thinking_delta(_piece):
                    yield c
            else:
                # NIM emits whitespace-only content deltas (e.g. a lone
                # newline after tool_calls). Without an open text block they
                # would open standalone whitespace text blocks that clients
                # render as junk lines (transcript: 47 occurrences).
                if not _piece.strip() and text_index is None:
                    continue
                async for c in emit_text(_piece):
                    yield c

    async def parse_and_emit(chunk, is_reasoning):
        nonlocal in_dsml_mode
        if in_dsml_mode:
            async for c in process_dsml(chunk):
                yield c
            return
        if is_reasoning:
            # Anti-trace (2026-10-15 forensics): reasoning_content is
            # upstream-extracted reasoning. Never marker-parse it: a literal
            # closing tag quoted inside the model's own reasoning split the
            # thinking block mid-sentence and leaked the remainder into
            # visible prose (reproduced live).
            async for c in emit_thinking_delta(chunk):
                yield c
            return
        # DSML tool markup diverts before tag filtering (unchanged behavior).
        dsml_start_idx = chunk.find('<|DSML|tool_calls>')
        if dsml_start_idx == -1:
            dsml_start_idx = chunk.find('｜DSML｜tool_calls>')
        if dsml_start_idx != -1:
            before, after = chunk[:dsml_start_idx], chunk[dsml_start_idx:]
            if before:
                async for c in _emit_filtered_content(before):
                    yield c
            in_dsml_mode = True
            async for c in process_dsml(after):
                yield c
            return
        async for c in _emit_filtered_content(chunk):
            yield c

'''
    text = text[:start] + new_block + text[end:]
    print('patched: parse_and_emit rewrite (anti-trace filter)')

    # ---- 4. tool-boundary flush ----
    replace_once(
        "                    if oi not in tool_map:\n"
        "                        if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:\n",
        "                    if oi not in tool_map:\n"
        "                        # Anti-trace: flush pending filtered content\n"
        "                        # (stray trailing think-tags stripped) before\n"
        "                        # the tool block opens.\n"
        "                        try:\n"
        "                            for _fkind, _fpiece in _think_filter.flush_tool_boundary():\n"
        "                                if not _fpiece:\n"
        "                                    continue\n"
        "                                if _fkind == 'thinking':\n"
        "                                    async for c in emit_thinking_delta(_fpiece):\n"
        "                                        yield c\n"
        "                                elif _fpiece.strip() or text_index is not None:\n"
        "                                    async for c in emit_text(_fpiece):\n"
        "                                        yield c\n"
        "                        except Exception:\n"
        "                            pass\n"
        "                        if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:\n",
        'tool-boundary-flush')

    # ---- 5. end-of-stream flush inside finally ----
    replace_once(
        "        if not client_gone:\n"
        "            if open_idx is not None and open_idx not in set(tool_map.values()):\n"
        "                try:\n"
        "                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n",
        "        if not client_gone:\n"
        "            # Anti-trace: final flush of filtered content. Trailing\n"
        "            # stray think-tags (NIM nemotron closing-tag artifact,\n"
        "            # transcript tails '...</think>\\n') never reach the client\n"
        "            # as visible text.\n"
        "            try:\n"
        "                for _fkind, _fpiece in _think_filter.close():\n"
        "                    if not _fpiece:\n"
        "                        continue\n"
        "                    if _fkind == 'thinking':\n"
        "                        async for c in emit_thinking_delta(_fpiece):\n"
        "                            yield c\n"
        "                    elif _fpiece.strip() or text_index is not None:\n"
        "                        async for c in emit_text(_fpiece):\n"
        "                            yield c\n"
        "            except Exception:\n"
        "                pass\n"
        "            if open_idx is not None and open_idx not in set(tool_map.values()):\n"
        "                try:\n"
        "                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n",
        'end-flush')

    # ---- 6. non-stream: strip stray trailing tags from extracted content ----
    replace_once(
        "    return {'reasoning': reasoning or '', 'content': content}\n",
        "    # Anti-trace: strip stray trailing think-tags (e.g. NIM's closing-tag\n"
        "    # artifact) from non-stream content so they never render as prose.\n"
        "    if isinstance(content, str):\n"
        "        content = strip_trailing_think_tags(content)\n"
        "    return {'reasoning': reasoning or '', 'content': content}\n",
        'non-stream-strip')

    SRC.write_text(text, encoding='utf-8')
    print('written')

    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
