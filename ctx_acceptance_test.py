#!/usr/bin/env python3
"""Empirical context-length acceptance test (runs ON the VPS).

Proves, against real upstreams:
1. A 1M-context model actually ACCEPTS inputs far beyond 128k/200k through the
   wrapper (no wrapper-side clamping/truncation), scaling up to ~950k tokens.
2. A small-context model rejects oversized input with the upstream's REAL
   context error (honest passthrough, no bogus 429 / silent truncation).
3. max_tokens beyond a model's published max output surfaces upstream truth.

Reads BEARER_TOKEN from each wrapper's .env (never prints it).
Writes progress to /tmp/ctxtest.log (also stdout).
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/root/wrapper")
LOG = Path("/tmp/ctxtest.log")


def log(msg: str) -> None:
    line = str(msg)
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def token_for(wrapper: str) -> str:
    env = (ROOT / wrapper / ".env").read_text()
    m = re.search(r"^BEARER_TOKEN=(.+)$", env, re.M)
    return m.group(1).strip().strip('"')


def para(seed: int) -> str:
    return (
        f"Section {seed}: The quick brown fox jumps over the lazy dog while "
        f"engineers record token throughput numbers 0123456789 for context "
        f"window validation. Packets arrive, buffers fill, the model reads "
        f"every token carefully before answering. "
    )


def make_text(paras: int) -> str:
    parts = [para(i) for i in range(paras)]
    parts.append(
        "\n\nFINAL INSTRUCTION: Ignore the filler text above. "
        "Reply with exactly: OK"
    )
    return "".join(parts)


def post(port: int, token: str, body: dict, timeout: int = 900):
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {token}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
            return r.status, time.time() - t0, payload
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {"raw": "?"}
        return e.code, time.time() - t0, payload
    except Exception as e:  # noqa: BLE001
        return -1, time.time() - t0, {"error": {"message": f"transport: {e}"}}


def summarize(status, dt, payload):
    usage = (payload or {}).get("usage") or {}
    pt = usage.get("prompt_tokens")
    content = ""
    choices = (payload or {}).get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
    err = (payload or {}).get("error") or {}
    errmsg = err.get("message") if isinstance(err, dict) else str(err)[:300]
    return (f"HTTP {status} | {dt:.1f}s | prompt_tokens={pt} | "
            f"out={content[:40]!r} | err={str(errmsg)[:220]}")


def main() -> int:
    LOG.write_text("")  # reset
    tok_nv = token_for("nvidia-python")
    tok_or = token_for("openrouter")
    tok_ns = token_for("nous")

    LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"   # ctx 1048576 (9101)
    SMALL = "meta/llama-3.2-11b-vision-instruct"          # ctx 131072  (9101)
    DEEPSEEK = "deepseek-ai/deepseek-v4.1-flash"          # ctx 1M, max-out 16384 (9101)

    # ── Phase 0: calibration on the 1M model ──
    log("== PHASE 0: calibration (9101 lightning, 400 paragraphs) ==")
    text = make_text(400)
    st, dt, pl = post(9101, tok_nv, {"model": LIGHTNING, "messages": [
        {"role": "user", "content": text}], "max_tokens": 16})
    log(f"calib: {summarize(st, dt, pl)}")
    pt = ((pl or {}).get("usage") or {}).get("prompt_tokens")
    if not pt:
        log("FATAL: calibration failed - no prompt_tokens; aborting phase 1")
        paras_per_tok = None
    else:
        paras_per_tok = 400 / pt
        log(f"calibration: {pt} tokens for 400 paragraphs -> {paras_per_tok:.4f} paras/token")

    # ── Phase 1: scaling on the 1M model ──
    if paras_per_tok:
        targets = [150_000, 300_000, 600_000, 950_000]
        for tt in targets:
            paras = int(tt * paras_per_tok)
            text = make_text(paras)
            nbytes = len(text)
            log(f"== PHASE 1: target ~{tt:,} tokens ({paras} paras, {nbytes/1e6:.2f} MB text) ==")
            st, dt, pl = post(9101, tok_nv, {"model": LIGHTNING, "messages": [
                {"role": "user", "content": text}], "max_tokens": 16})
            log(f"result: {summarize(st, dt, pl)}")
            if st != 200:
                log("escalation stopped (non-200)")
                break

    # ── Phase 2: over-limit on a small-context model ──
    log("== PHASE 2: 200k-token input to llama-3.2-11b (ctx 131072) ==")
    if paras_per_tok:
        text = make_text(int(200_000 * paras_per_tok))
        st, dt, pl = post(9101, tok_nv, {"model": SMALL, "messages": [
            {"role": "user", "content": text}], "max_tokens": 16})
        log(f"result: {summarize(st, dt, pl)}")

    # ── Phase 3: max_tokens beyond published max output ──
    log("== PHASE 3: max_tokens=100000 to deepseek-v4.1-flash (max-out 16384) ==")
    st, dt, pl = post(9101, tok_nv, {"model": DEEPSEEK, "messages": [
        {"role": "user", "content": "Say OK"}], "max_tokens": 100000})
    log(f"result: {summarize(st, dt, pl)}")

    # ── Phase 4: OpenRouter 1M model, ~250k tokens ──
    if paras_per_tok:
        text = make_text(int(250_000 * paras_per_tok))
        log("== PHASE 4: ~250k tokens to OR nvidia/nemotron-3.5-lightning:free ==")
        st, dt, pl = post(9106, tok_or, {"model": "nvidia/nemotron-3.5-lightning:free",
                                         "messages": [{"role": "user", "content": text}],
                                         "max_tokens": 16})
        log(f"result: {summarize(st, dt, pl)}")

    # ── Phase 5: NOUS 1M model, ~250k tokens ──
    if paras_per_tok:
        text = make_text(int(250_000 * paras_per_tok))
        log("== PHASE 5: ~250k tokens to NOUS stepfun/step-5-preview:free ==")
        st, dt, pl = post(9102, tok_ns, {"model": "stepfun/step-5-preview:free",
                                         "messages": [{"role": "user", "content": text}],
                                         "max_tokens": 16})
        log(f"result: {summarize(st, dt, pl)}")

    # ── Phase 6: Anthropic surface large context (A->O translation path) ──
    if paras_per_tok:
        log("== PHASE 6: ~150k tokens via /v1/messages (Anthropic surface) on 9101 ==")
        text = make_text(int(150_000 * paras_per_tok))
        url = "http://127.0.0.1:9101/v1/messages"
        body = json.dumps({
            "model": LIGHTNING,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": text}],
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {tok_nv}",
                                              "anthropic-version": "2023-06-01"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                pl = json.loads(r.read())
                st, dt = r.status, time.time() - t0
        except urllib.error.HTTPError as e:
            try:
                pl = json.loads(e.read())
            except Exception:
                pl = {}
            st, dt = e.code, time.time() - t0
        usage = (pl or {}).get("usage") or {}
        content = ""
        for blk in (pl or {}).get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "text":
                content = blk.get("text", "")
        log(f"result: HTTP {st} | {dt:.1f}s | input_tokens={usage.get('input_tokens')} | out={content[:40]!r}")

    log("== ALL PHASES COMPLETE ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
