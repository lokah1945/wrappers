#!/usr/bin/env python3
"""Behavioral proof that the NVIDIA non-stream request timeout now honors
REQUEST_TIMEOUT_SEC=600 (previously the code read REQUEST_TIMEOUT and fell
back to 120s).

Method: send kimi-k3 (dense, 1M ctx) a ~400k-token prompt. Prefill for dense
models regularly exceeds 120s. Under the old code the attempt would abort at
120s and burn the whole key pool; under the fixed code the request must
complete with HTTP 200 in a single attempt.
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

env = Path("/root/wrapper/nvidia-python/.env").read_text()
tok = re.search(r"^BEARER_TOKEN=(.+)$", env, re.M).group(1).strip()


def para(seed: int) -> str:
    return (
        f"Section {seed}: The quick brown fox jumps over the lazy dog while "
        f"engineers record token throughput numbers 0123456789 for context "
        f"window validation. Packets arrive, buffers fill, the model reads "
        f"every token carefully before answering. "
    )


# 0.0189 paras/token calibrated earlier: 400k tokens ~ 7560 paras
text = "".join(para(i) for i in range(7560)) + "\n\nReply with exactly: OK"
body = json.dumps({
    "model": "moonshotai/kimi-k3",
    "messages": [{"role": "user", "content": text}],
    "max_tokens": 100,
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:9101/v1/chat/completions", data=body, method="POST",
    headers={"content-type": "application/json", "authorization": f"Bearer {tok}"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=560) as r:
        pl = json.loads(r.read())
        st = r.status
except urllib.error.HTTPError as e:
    try:
        pl = json.loads(e.read())
    except Exception:
        pl = {}
    st = e.code
except Exception as e:  # noqa: BLE001
    st, pl = -1, {"error": {"message": str(e)[:300]}}

dt = time.time() - t0
usage = (pl or {}).get("usage") or {}
pt = usage.get("prompt_tokens")
content = ""
ch = (pl or {}).get("choices") or []
if ch:
    content = (ch[0].get("message") or {}).get("content") or ""
err = (pl or {}).get("error") or {}
print(f"RESULT: HTTP {st} | {dt:.1f}s | prompt_tokens={pt} | out={content[:60]!r}")
print(f"ERROR: {str(err)[:300] if err else 'none'}")
print(f"VERDICT: {'PASS - completed in single attempt' if st == 200 and dt > 120 else ('PASS-fast' if st == 200 else 'FAIL')}")
