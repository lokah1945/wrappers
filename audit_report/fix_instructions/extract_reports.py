#!/usr/bin/env python3
"""Extract final implementation reports from fix-agent transcripts."""
import json

D = '/root/.claude/projects/-root-wrapper/7b9ccda8-9536-4e6c-94d8-8833791f7433/subagents'
AGENTS = [
    ('a6eac510f9b0ff2b1', 'nvidia-python'),
    ('a27e2637ad46df8e7', 'nous'),
    ('a3ee94ef1d66b41be', 'opencode'),
    ('a9b2b80697538748c', 'blackbox'),
]

out = open('/root/wrapper/audit_report/parts/2026-07-27_fix_implementation_reports.md', 'w')
out.write('# Fix Implementation Reports — 2026-07-27 (per fix-agent)\n')
for aid, name in AGENTS:
    best = None
    for line in open(f'{D}/agent-{aid}.jsonl'):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get('type') == 'assistant':
            for c in j.get('message', {}).get('content', []):
                if isinstance(c, dict) and c.get('type') == 'text':
                    t = c.get('text', '').strip()
                    if len(t) > 1500:
                        best = t
    out.write(f'\n\n---\n\n## Component: {name}\n\n')
    out.write(best if best else '(no long-form report found)')
out.close()
print('written')
