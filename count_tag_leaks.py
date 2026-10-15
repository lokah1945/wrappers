#!/usr/bin/env python3
"""Count literal thinking-tag leaks in text blocks across transcripts."""
import json
from pathlib import Path

PROJ = Path('/root/.claude/projects/-root-wrapper')


def main():
    totals = {}
    for p in sorted(PROJ.glob('*.jsonl')):
        stats = {'text': 0, 'end_close_tag': 0, 'any_close_tag': 0, 'any_open_tag': 0,
                 'any_lhs': 0, 'nl_only': 0, 'think_frag': 0, 'msgs': 0}
        examples = []
        prev_type = None
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('type') != 'assistant':
                prev_type = None
                continue
            msg = rec.get('message') or {}
            stats['msgs'] += 1
            blocks = msg.get('content') or []
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = b.get('type')
                if t == 'text':
                    txt = b.get('text') or ''
                    stats['text'] += 1
                    stripped = txt.rstrip()
                    if stripped.endswith('</thinking>'):
                        stats['end_close_tag'] += 1
                        if len(examples) < 2:
                            examples.append(txt[-80:])
                    if '</thinking>' in txt:
                        stats['any_close_tag'] += 1
                    if '<thinking>' in txt:
                        stats['any_open_tag'] += 1
                    if '\ufffd' in txt:
                        stats['any_lhs'] += 1
                    if txt.strip() == '' and txt:
                        stats['nl_only'] += 1
                elif t == 'thinking':
                    th = b.get('thinking') or ''
                    if th and (th[-1] not in '.!?"\'\n ' and len(th) > 30):
                        stats['think_frag'] += 1
                prev_type = t
        print(f"{p.name[:12]}: msgs={stats['msgs']} text={stats['text']} "
              f"endClose={stats['end_close_tag']} anyClose={stats['any_close_tag']} "
              f"anyOpen={stats['any_open_tag']} lhs={stats['any_lhs']} "
              f"nlOnly={stats['nl_only']} thinkFrag={stats['think_frag']}")
        for ex in examples:
            print(f"    example tail: {ex!r}")
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
    print('TOTALS:', totals)


if __name__ == '__main__':
    main()
