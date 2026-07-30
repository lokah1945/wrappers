#!/usr/bin/env python3
"""Test all text-to-text LLM chat models on wrapper-nvidia (port 9101) with 'hallo'."""
import json, urllib.request, urllib.error, time, sys

BASE = "http://127.0.0.1:9101"
TOK = "wrapper-local-key"

# Substrings that indicate NON-text-to-text models (skip)
SKIP = (
    'embed', 'clip', 'flux', 'stable-diffusion', 'qwen-image', 'diffusion',
    'consistory', 'kandinsky', 'playground', 'vision', '/vl', 'vila', 'neva',
    'guard', 'safety', 'reward', 'parse', 'retriever', 'translate', 'audio',
    'fugatto', 'kosmos', 'deplot', 'recurrentgemma', 'synthetic-video',
    'ising-calibration', 'cosmos', 'codegemma', 'nemoguard', 'nemoretriever',
)

def is_text_llm(mid):
    low = mid.lower()
    if any(s in low for s in SKIP):
        return False
    return True

def chat(model, timeout=25):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hallo"}],
        "max_tokens": 60,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {TOK}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
            if "choices" in d and d["choices"]:
                c = d["choices"][0]["message"]["content"]
                return ("OK", c[:200], d["choices"][0].get("finish_reason"))
            if "error" in d:
                return ("ERR", str(d["error"].get("message", ""))[:200], d["error"].get("code"))
            return ("UNK", str(d)[:200], None)
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
            return ("ERR", str(d.get("error", {}).get("message", ""))[:200], e.code)
        except Exception:
            return ("HTTP", str(e)[:150], e.code)
    except Exception as e:
        return ("FAIL", str(e)[:150], None)

def main():
    # fetch models
    req = urllib.request.Request(f"{BASE}/v1/models")
    req.add_header("Authorization", f"Bearer {TOK}")
    with urllib.request.urlopen(req, timeout=10) as r:
        models = json.loads(r.read().decode()).get("data", [])
    ids = [m["id"] for m in models]
    text_llms = [i for i in ids if is_text_llm(i)]
    print(f"TOTAL models: {len(ids)} | text-to-text LLMs to test: {len(text_llms)}", flush=True)

    results = []
    for i, mid in enumerate(text_llms, 1):
        status, msg, code = chat(mid)
        results.append({"model": mid, "status": status, "reply": msg, "code": code})
        tag = "✅" if status == "OK" else "❌"
        print(f"[{i}/{len(text_llms)}] {tag} {mid}: {status} | {msg[:80]}", flush=True)
        time.sleep(0.3)  # gentle pace

    # write report
    ok = [r for r in results if r["status"] == "OK"]
    err = [r for r in results if r["status"] != "OK"]
    with open("/root/wrapper/nvidia_llm_test_report.json", "w") as f:
        json.dump({"total_tested": len(results), "ok": len(ok), "failed": len(err),
                   "results": results}, f, indent=2)
    print(f"\n=== SUMMARY ===\ntested={len(results)} ok={len(ok)} failed={len(err)}", flush=True)
    print("Report written to /root/wrapper/nvidia_llm_test_report.json", flush=True)

if __name__ == "__main__":
    main()
