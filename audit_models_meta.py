#!/usr/bin/env python3
"""Run ON VPS. E2E audit: model list METADATA quality per wrapper.

For each active wrapper + registry: /v1/models -> field inventory,
completeness ratio, full sample entries. Never prints secrets.
"""
import json
import re
from pathlib import Path

import urllib.request

ROOT = Path("/root/wrapper")
WRAPPERS = ["nvidia-python", "nous", "openrouter"]
PORTS = {"nvidia-python": 9101, "nous": 9102, "openrouter": 9106}


def token_for(name):
    p = ROOT / name / ".env"
    if not p.exists():
        return ""
    m = re.search(r"^BEARER_TOKEN=(.+)$", p.read_text(), re.M)
    return m.group(1).strip() if m else ""


def get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = None
        return e.code, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def audit(label, url, token=None):
    print("=" * 78)
    print(f"[{label}] {url}")
    st, data = get(url, token)
    if st != 200 or not isinstance(data, dict):
        print(f"  !! HTTP {st}: {str(data)[:300]}")
        return None
    models = data.get("data") or data.get("models") or []
    print(f"  HTTP 200, total models: {len(models)}, top-level keys: {sorted(data.keys())}")
    if not models:
        print(f"  RAW: {str(data)[:300]}")
        return models
    fields = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            f = fields.setdefault(k, [0, 0, None])  # present, nonempty, sample
            f[0] += 1
            ne = v is not None and v != "" and v != [] and v != {}
            if ne:
                f[1] += 1
                if f[2] is None:
                    s = v if isinstance(v, (int, float, bool, str)) else json.dumps(v)
                    f[2] = s if len(str(s)) <= 85 else str(s)[:85] + "..."
    print(f"  {'field':30} {'nonempty':>10} {'/':>3} {'present':>8}  sample")
    for k in sorted(fields):
        p, n, s = fields[k]
        print(f"  {k:30} {n:>10} {'/':>3} {p:>8}  {s}")
    print("  --- entry[0] full ---")
    print("  " + json.dumps(models[0], indent=2)[:1500].replace("\n", "\n  "))
    print("  --- entry[mid] full ---")
    print("  " + json.dumps(models[len(models) // 2], indent=2)[:1500].replace("\n", "\n  "))
    print("  --- entry[last] full ---")
    print("  " + json.dumps(models[-1], indent=2)[:1500].replace("\n", "\n  "))
    return models


def main():
    out = {}
    for w in WRAPPERS:
        out[w] = audit(w.upper(), f"http://127.0.0.1:{PORTS[w]}/v1/models", token_for(w))
    out["registry"] = audit("REGISTRY", "http://127.0.0.1:9200/v1/models")
    # save raw for local deep-dive
    blob = {}
    for label, models in out.items():
        blob[label] = models
    Path("/root/wrapper/.audit_models_raw.json").write_text(json.dumps(blob, indent=1))
    print("=" * 78)
    print("raw snapshot -> /root/wrapper/.audit_models_raw.json")


if __name__ == "__main__":
    main()
