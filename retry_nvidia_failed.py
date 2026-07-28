#!/usr/bin/env python3
"""Retry failed nvidia-python LLM models with stream:true + larger timeout."""
import json, urllib.request, urllib.error, time

BASE = "http://127.0.0.1:9101"
TOK = "wrapper-local-key"

def chat(model, timeout=60):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hallo"}],
        "max_tokens": 150,
        "temperature": 0.3,
        "stream": True,   # NVIDIA NIM often needs stream for completion
    }).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {TOK}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # read SSE, collect content deltas
            buf = ""
            chars = 0
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[5:].strip())
                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            chars += len(delta)
                            buf += delta
                    except Exception:
                        pass
            if chars > 0:
                return ("OK", buf[:200], chars)
            return ("EMPTY", "(stream ended no content)", 0)
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
            return ("ERR", str(d.get("error", {}).get("message", ""))[:180], e.code)
        except Exception:
            return ("HTTP", str(e)[:120], e.code)
    except Exception as e:
        return ("FAIL", str(e)[:120], None)

def main():
    rep = json.load(open("/root/wrapper/nvidia_llm_test_report.json"))
    failed = [r["model"] for r in rep["results"] if r["status"] != "OK"]
    print(f"Retrying {len(failed)} failed models with stream:true, timeout=60s", flush=True)
    results = []
    for i, mid in enumerate(failed, 1):
        st, msg, code = chat(mid)
        results.append({"model": mid, "status": st, "reply": msg, "code": code})
        tag = "✅" if st == "OK" else ("⚠️" if st == "EMPTY" else "❌")
        print(f"[{i}/{len(failed)}] {tag} {mid}: {st} | {msg[:70]}", flush=True)
        time.sleep(0.4)
    ok = [r for r in results if r["status"] == "OK"]
    empty = [r for r in results if r["status"] == "EMPTY"]
    err = [r for r in results if r["status"] not in ("OK", "EMPTY")]
    with open("/root/wrapper/nvidia_retry_report.json", "w") as f:
        json.dump({"retried": len(results), "ok_stream": len(ok),
                   "empty_stream": len(empty), "still_failed": len(err),
                   "results": results}, f, indent=2)
    print(f"\n=== RETRY SUMMARY ===\nretried={len(results)} ok_stream={len(ok)} empty={len(empty)} still_failed={len(err)}", flush=True)

if __name__ == "__main__":
    main()
