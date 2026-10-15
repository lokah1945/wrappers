#!/usr/bin/env python3
"""Key rotation & rate-limit health probe across active wrappers (v2)."""
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path("/root/wrapper")


def token(wrapper: str) -> str:
    env = (ROOT / wrapper / ".env").read_text()
    return re.search(r"^BEARER_TOKEN=(.+)$", env, re.M).group(1).strip()


for port, name in ((9101, "nvidia-python"), (9102, "nous"), (9106, "openrouter")):
    print(f"=== {name} (port {port}) ===")
    headers = {"authorization": f"Bearer {token(name)}"}
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/metrics/keys", headers=headers),
            timeout=6).read()
        d = json.loads(raw)
    except Exception as e:
        print(f"  endpoint unavailable: {e}")
        continue
    ks = d.get("keys") or []
    total = 0
    for k in ks[:8]:
        live = k.get("live") or {}
        total += k.get("requests", 0)
        print(f"  {k.get('key_label', '?'):10} req={k.get('requests', 0):4} "
              f"429={k.get('rate_limited_count', 0):2} "
              f"retry={k.get('total_retries', 0):2} "
              f"inflight={live.get('in_flight', 0)} "
              f"hardblk={live.get('hard_blocked', False)} "
              f"rpm={live.get('current_rpm', 0)}")
    if ks:
        per = [k.get("requests", 0) for k in ks]
        spread = max(per) - min(per) if per else 0
        print(f"  -> {len(ks)} keys, total req {total}, spread max-min={spread} "
              f"({'even' if spread <= max(2, total * 0.35) else 'SKEWED'})")
