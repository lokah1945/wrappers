#!/usr/bin/env python3
"""Patch common/translations/shared.py: CRIT-03 anti-trace fix.

Old behavior: non-greedy regex extracted <thinking>...</thinking> anywhere in
content into a thinking block, but the TEXT still used the raw content — the
tags leaked into visible text AND the extracted fragment duplicated.
New behavior: start-anchored extraction (opener at content start only), the
extracted span removed from text, trailing stray closers stripped.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/common/translations/shared.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if 'extract_inline_thinking' in text:
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

    # ---- 1. import (package-relative with standalone fallback) ----
    anchor_import = "def parse_dsml_from_text(text: str)"
    replace_once(
        anchor_import,
        "try:\n"
        "    from .think_tags import extract_inline_thinking, strip_trailing_think_tags\n"
        "except ImportError:  # pragma: no cover - standalone use outside the package\n"
        "    from think_tags import extract_inline_thinking, strip_trailing_think_tags\n"
        "\n\n"
        "def parse_dsml_from_text(text: str)",
        'import')

    # ---- 2. CRIT-03 block + text content ----
    old_block = (
        "    # CRIT-03: Extract thinking from content field when reasoning_content is not provided.\n"
        "    # Nemotron models (e.g. nvidia/nemotron-3-ultra-550b-a55b) embed\n"
        "    # <thinking>...</thinking> tags in the content string instead of using\n"
        "    # a separate reasoning_content field. Without this extraction, the tags\n"
        "    # pass through into visible text output for the client.\n"
        "    if not reasoning and isinstance(msg.get(\"content\"), str):\n"
        "        m = re.search(r'<thinking>([\\s\\S]*?)</thinking>', msg[\"content\"])\n"
        "        if m:\n"
        "            reasoning = m.group(1).strip()\n"
        "            content.append({\"type\": \"thinking\", \"thinking\": reasoning})\n"
        "\n"
        "    # Text content\n"
        "    raw_text = msg.get(\"content\") or \"\"\n"
        "    if raw_text:\n"
    )
    new_block = (
        "    # CRIT-03 + anti-trace (2026-10-15 forensics): inline thinking\n"
        "    # extraction is START-ANCHORED (only when the content begins with an\n"
        "    # opener tag), the extracted span is REMOVED from the visible text,\n"
        "    # and trailing stray closers are stripped. The previous non-greedy\n"
        "    # regex matched literal mid-prose tag mentions, duplicated a\n"
        "    # fragment into thinking AND left the raw tags in the text.\n"
        "    raw_text = msg.get(\"content\") or \"\"\n"
        "    if isinstance(raw_text, str):\n"
        "        if not reasoning:\n"
        "            inline_thinking, raw_text = extract_inline_thinking(raw_text)\n"
        "            if inline_thinking:\n"
        "                reasoning = inline_thinking\n"
        "                content.append({\"type\": \"thinking\", \"thinking\": reasoning})\n"
        "        else:\n"
        "            raw_text = strip_trailing_think_tags(raw_text)\n"
        "\n"
        "    # Text content\n"
        "    if raw_text:\n"
    )
    replace_once(old_block, new_block, 'crit03-antitrace')

    SRC.write_text(text, encoding='utf-8')
    print('written')

    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
