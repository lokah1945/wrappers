#!/usr/bin/env python3
"""Forensik percakapan Claude Code: struktur blok konten persis dari wrapper.

Memeriksa: model, blok thinking (signature?), blok text, jejak tag/artejak,
dan apakah history replay user memuat blok thinking (bukti signature OK).
"""
import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/.claude/projects/-root-wrapper/53fab01f-4d2d-4cc2-977c-e6df38d04962.jsonl"

BAD_PATTERNS = ["<think>", "</think>", "<thinking>", "</thinking>", "```thinking"]

def clip(s, n=90):
    s = s.replace("\n", "\\n")
    return (s[:n] + "...") if len(s) > n else s

def blocks_info(content):
    out = []
    if isinstance(content, str):
        out.append(("str", len(content), clip(content)))
        return out
    if not isinstance(content, list):
        out.append((type(content).__name__, "-", "-"))
        return out
    for b in content:
        if not isinstance(b, dict):
            out.append((type(b).__name__, "-", "-"))
            continue
        t = b.get("type", "?")
        if t == "thinking":
            sig = b.get("signature")
            sig_desc = "MISSING" if sig is None else f"present(len={len(sig)}, head={clip(sig, 24)!r})"
            out.append((f"thinking[{b.get('thinking', '') and len(b.get('thinking',''))}ch sig={sig_desc}]", "-", clip(b.get("thinking", ""))))
        elif t == "text":
            txt = b.get("text", "")
            out.append((f"text[{len(txt)}ch]", "-", clip(txt)))
        elif t == "tool_use":
            out.append((f"tool_use(name={b.get('name')})", "-", "-"))
        elif t == "tool_result":
            out.append((f"tool_result", "-", "-"))
        else:
            out.append((t, "-", "-"))
    return out

def scan_traces(content):
    hits = []
    def scan(s, where):
        if not isinstance(s, str):
            return
        for p in BAD_PATTERNS:
            if p in s:
                hits.append((where, p, clip(s, 120)))
    if isinstance(content, list):
        for i, b in enumerate(content):
            if isinstance(b, dict):
                for k in ("text", "thinking"):
                    if k in b:
                        scan(b[k], f"block[{i}].{k}")
                if "content" in b:
                    scan(b["content"], f"block[{i}].content")
    elif isinstance(content, str):
        scan(content, "str-content")
    return hits

with open(PATH) as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception as e:
            print(f"L{i}: PARSE-ERROR {e}")
            continue
        rtype = rec.get("type")
        ts = rec.get("timestamp", "")
        msg = rec.get("message") or {}
        if rtype in ("user", "assistant"):
            content = msg.get("content")
            print(f"\n=== L{i} type={rtype} ts={ts} model={msg.get('model','-')} stop={msg.get('stop_reason','-')}")
            for label, _, preview in blocks_info(content):
                print(f"    [{label}] {preview!r}")
            for where, p, prev in scan_traces(content):
                print(f"    !!! TRACE {p} in {where}: {prev!r}")
            usage = msg.get("usage") or {}
            if usage:
                print(f"    usage: in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
        elif rtype == "summary":
            print(f"\n=== L{i} SUMMARY: {clip(rec.get('summary',''), 100)}")
        else:
            print(f"\n=== L{i} type={rtype} ts={ts} (keys={list(rec.keys())[:8]})")

print("\n--- SELESAI ---")
