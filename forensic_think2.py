#!/usr/bin/env python3
"""Dump codepoint eksak dari blok text assistant percakapan Claude Code."""
import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/.claude/projects/-root-wrapper/53fab01f-4d2d-4cc2-977c-e6df38d04962.jsonl"

def codepoints(s, center_idx, before=30, after=40):
    lo = max(0, center_idx - before)
    hi = min(len(s), center_idx + after)
    out = []
    for i, ch in enumerate(s[lo:hi], start=lo):
        disp = ch if (ch.isprintable() and ch not in "\r\n") else "?"
        out.append(f"{i}:{ord(ch):#06x}({disp})")
    return " ".join(out)

with open(PATH) as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content") or []
        for j, b in enumerate(content if isinstance(content, list) else []):
            if isinstance(b, dict) and b.get("type") == "text":
                txt = b.get("text", "")
                print(f"\n=== L{i} block[{j}] len={len(txt)} FULL TEXT:")
                print(repr(txt))
                # temukan posisi penutup think-tag
                for marker in ("</think>", "```END"):
                    pos = txt.find(marker)
                    if pos >= 0:
                        print(f"  marker {marker!r} @ pos {pos}:")
                        print("   " + codepoints(txt, pos, 20, len(marker) + 25))
                        # juga region sebelum marker (akhir reasoning)
                        print("   tail-of-reasoning: " + codepoints(txt, pos, 12, 0))
                        break
print("\n--- SELESAI ---")
