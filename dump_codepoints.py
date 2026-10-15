#!/usr/bin/env python3
"""Dump exact codepoints of artifact text tails from transcripts."""
import json
from pathlib import Path

PROJ = Path('/root/.claude/projects/-root-wrapper')


def main():
    path = PROJ / '82e56877-7f33-44d2-85e6-9c38c5638b05.jsonl'
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    for lineno in (18, 66, 44, 71):
        rec = json.loads(lines[lineno - 1].strip())
        msg = rec.get('message') or {}
        for b in (msg.get('content') or []):
            if isinstance(b, dict) and b.get('type') == 'text':
                t = b.get('text') or ''
                out = [f'L{lineno} len={len(t)}']
                tail = t[-12:]
                out.append('  tail codepoints: ' + ' '.join(f'U+{ord(c):04X}' for c in tail))
                out.append('  tail repr: ' + ascii(tail))
                print('\n'.join(out))


if __name__ == '__main__':
    main()
