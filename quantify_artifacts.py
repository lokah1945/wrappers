#!/usr/bin/env python3
"""Quantify artifact patterns across all Claude Code transcripts."""
import json
import re
from pathlib import Path

PROJ = Path('/root/.claude/projects/-root-wrapper')


def scan(path):
    stats = {
        'assistant_msgs': 0, 'text_blocks': 0, 'thinking_blocks': 0,
        'text_end_fences': 0, 'text_nl_only': 0, 'text_lead_nl': 0,
        'think_truncated_look': 0, 'models': set(), 'fence_examples': [],
        'nl_after_tool': 0,
    }
    prev_had_tool = False
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('type') != 'assistant':
                prev_had_tool = False
                continue
            msg = rec.get('message') or {}
            stats['models'].add(msg.get('model', '?'))
            stats['assistant_msgs'] += 1
            blocks = msg.get('content') or []
            if not isinstance(blocks, list):
                continue
            for i, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                t = b.get('type')
                if t == 'text':
                    stats['text_blocks'] += 1
                    txt = b.get('text') or ''
                    if txt.rstrip().endswith('```') or txt.endswith('```'):
                        stats['text_end_fences'] += 1
                        if len(stats['fence_examples']) < 3:
                            stats['fence_examples'].append((path.name, txt[:120]))
                    if txt.strip() == '' and txt:
                        stats['text_nl_only'] += 1
                        if prev_had_tool:
                            stats['nl_after_tool'] += 1
                    if txt.startswith('\n') and len(txt) > 1:
                        stats['text_lead_nl'] += 1
                    prev_had_tool = False
                elif t == 'thinking':
                    stats['thinking_blocks'] += 1
                    th = b.get('thinking') or ''
                    # heuristic: mid-sentence start = suspicious extraction
                    if th and th[:1].islower() and not th[:1].isspace():
                        stats['think_truncated_look'] += 1
                    prev_had_tool = False
                elif t == 'tool_use':
                    prev_had_tool = True
    return stats


def main():
    total = {}
    for p in sorted(PROJ.glob('*.jsonl')):
        try:
            s = scan(p)
        except Exception as e:
            print(f'{p.name}: ERROR {e}')
            continue
        print(f"{p.name}: msgs={s['assistant_msgs']} text={s['text_blocks']} "
              f"think={s['thinking_blocks']} endFence={s['text_end_fences']} "
              f"nlOnly={s['text_nl_only']} nlAfterTool={s['nl_after_tool']} "
              f"leadNL={s['text_lead_nl']} thinkFrag={s['think_truncated_look']}")
        for k in ('assistant_msgs', 'text_blocks', 'thinking_blocks', 'text_end_fences',
                  'text_nl_only', 'text_lead_nl', 'think_truncated_look', 'nl_after_tool'):
            total[k] = total.get(k, 0) + s[k]
        for ex in s['fence_examples']:
            print(f"   fenceExample: {ex[0]}: {ex[1]!r}")
    print('TOTAL:', total)


if __name__ == '__main__':
    main()
