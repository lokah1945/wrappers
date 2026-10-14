#!/usr/bin/env python3
"""Forensic scan of Claude Code conversation logs for wrapper translation artifacts.

Scans .jsonl transcripts for content-block anomalies that indicate translation
bugs: raw ``` fences, type-anomalous blocks, empty/whitespace text blocks,
consecutive same-type text blocks, leaked reasoning, escape artifacts.
"""
import json
import re
import sys
from pathlib import Path

VALID_TYPES = {'text', 'thinking', 'tool_use', 'tool_result', 'image', 'document',
               'server_tool_use', 'web_search_tool_result', 'code_execution_tool_result',
               'container_upload'}

# Suspicious artifacts inside text blocks
PATTERNS = {
    'raw_code_fence_only': re.compile(r'^\s*```[a-zA-Z0-9_+-]*\s*$'),
    'orphan_closing_fence': re.compile(r'^\s*```\s*$'),
    'looks_like_json_frag': re.compile(r'^\s*[{}\[\]"\',:0-9]\s*$'),
    'anthropic_field_leak': re.compile(r'\b(content_block_start|content_block_delta|content_block_stop|message_start|message_delta|message_stop|input_json_delta|thinking_delta|text_delta)\b'),
    'openai_field_leak': re.compile(r'\b(finish_reason|completion_tokens|prompt_tokens|chatcmpl-|delta\.content|choices\[)\b'),
    'control_chars': re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]'),
    'unicode_esc_leak': re.compile(r'\\u[0-9a-fA-F]{4}\\u[0-9a-fA-F]{4}'),
    'signature_leak': re.compile(r'signature[":= ]'),
}


def blocks_of(msg):
    content = msg.get('message', {}).get('content')
    if isinstance(content, list):
        return content
    return []


def scan_file(path):
    findings = []
    n_lines = 0
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                rec = json.loads(line)
            except ValueError:
                findings.append((lineno, 'unparsable_line', '', line[:120]))
                continue
            if rec.get('type') not in ('assistant', 'user'):
                continue
            msg = rec.get('message') or {}
            role = msg.get('role')
            blocks = blocks_of(rec)
            # consecutive same-type text blocks
            prev_type = None
            for idx, b in enumerate(blocks):
                btype = b.get('type') if isinstance(b, dict) else type(b).__name__
                if btype not in VALID_TYPES:
                    findings.append((lineno, f'bad_type:{btype}', role, json.dumps(b)[:200]))
                if isinstance(b, dict) and btype == 'text':
                    txt = b.get('text') or ''
                    if not txt.strip():
                        findings.append((lineno, 'empty_text_block', role, json.dumps(b)[:200]))
                    for pname, pat in PATTERNS.items():
                        if pat.search(txt):
                            snippet = txt[:160].replace('\n', '\\n')
                            findings.append((lineno, f'pattern:{pname}', role, snippet))
                    if btype == prev_type == 'text':
                        findings.append((lineno, 'consecutive_text', role, txt[:80]))
                prev_type = btype
            # tool_result with string content (Claude Code tolerates, Anthropic spec wants blocks)
            for idx, b in enumerate(blocks):
                if isinstance(b, dict) and b.get('type') == 'tool_result':
                    c = b.get('content')
                    if isinstance(c, str) and len(c) > 0:
                        findings.append((lineno, 'tool_result_plain_string', role, c[:120]))
            # non-list content on assistant messages
            if role == 'assistant':
                c = msg.get('content')
                if isinstance(c, str):
                    findings.append((lineno, 'assistant_content_string', role, c[:120]))
    return n_lines, findings


def main():
    paths = sys.argv[1:]
    total = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            print(f'MISSING {p}')
            continue
        n, findings = scan_file(p)
        print(f'== {p.name}: {n} lines, {len(findings)} findings ==')
        for lineno, kind, role, snippet in findings[:200]:
            print(f'  L{lineno} [{role}] {kind}: {snippet}')
        total += len(findings)
    print(f'TOTAL findings: {total}')


if __name__ == '__main__':
    main()
