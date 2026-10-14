#!/usr/bin/env python3
"""Run ON VPS. Verify metadata fixes live: NOUS, OpenRouter, Registry."""
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path("/root/wrapper")


def token_for(name):
    env = (ROOT / name / ".env").read_text()
    m = re.search(r"^BEARER_TOKEN=(.+)$", env, re.M)
    return m.group(1).strip() if m else ""


def get(url, tok=None):
    req = urllib.request.Request(url)
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


print("=== NOUS (9102) ===")
d = get("http://127.0.0.1:9102/v1/models", token_for("nous"))
data = d.get("data") or []
print(f"models: {len(data)}")
for m in data:
    arch = m.get("architecture") or {}
    print(f"  {m['id']:42} in={m.get('input_modalities')} out={m.get('output_modalities')}"
          f" vision={m.get('supports_vision')} arch_in={arch.get('input_modalities')}")

print()
print("=== OPENROUTER (9106) ===")
d = get("http://127.0.0.1:9106/v1/models", token_for("openrouter"))
ids = sorted(m["id"] for m in d.get("data") or [])
print(f"models: {len(ids)}")
for i in ids:
    print("  ", i)

print()
print("=== REGISTRY (9200) capabilities ===")
for prov in ("nvidia", "openrouter", "nous"):
    d = get(f"http://127.0.0.1:9200/v1/models?provider={prov}")
    entries = d.get("data") or []
    with_caps = 0
    with_in_mods = 0
    sample = None
    for e in entries:
        caps = e.get("capabilities") or {}
        if any(v for v in caps.values() if v not in (None, (), False)):
            with_caps += 1
        if caps.get("input_modalities"):
            with_in_mods += 1
            if sample is None and len(caps["input_modalities"]) > 1:
                sample = (e["provider_model_id"], caps["input_modalities"], caps.get("vision"))
    print(f"  {prov}: {len(entries)} profiles | non-empty caps: {with_caps} | with input_modalities: {with_in_mods} | sample: {sample}")

print()
print("=== REGISTRY specific models ===")
for cid in ("nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "openrouter/google/lyria-3-pro-preview",
            "openrouter/openrouter/free",
            "nous/stepfun/step-5-preview:free"):
    try:
        e = get("http://127.0.0.1:9200/v1/models/" + cid)
        caps = e.get("capabilities") or {}
        print(f"  {cid}")
        print(f"    caps: { {k: v for k, v in caps.items() if v not in (None, (), False)} }")
    except Exception as ex:
        print(f"  {cid} -> ERR {str(ex)[:70]}")
