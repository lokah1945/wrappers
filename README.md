# Wrappers

Production-grade API proxies for various LLM providers.

This monorepo contains hardened, SDK-compatible transparent proxies that add multi-key rotation, pacing, metrics, streaming reliability, and full OpenAI + Anthropic compatibility.

## Current Status (2026-07-23)

| Wrapper            | Status          | Score   | Canonical Dir          | Notes |
|--------------------|-----------------|---------|------------------------|-------|
| **wrapper-nvidia** | ✅ Production   | **100/100** | `nvidia-python/`      | **Use this** for NVIDIA NIM. Node.js version in `nvidia/` is **deprecated**. |
| **wrapper-nous**   | ✅ Production   | **100/100** | `nous/`               | Nous Research inference API. |
| **wrapper-opencode** | ✅ Production | **100/100** | `opencode/`           | OpenCode **Zen** gateway (`https://opencode.ai/zen/v1`) — multi-protocol. |

## Repository Layout

```
wrappers/
├── README.md
├── .env.example
├── install.sh
├── wrapper-nvidia.service
├── CHANGELOG.md
│
├── nvidia-python/          # ← CANONICAL wrapper-nvidia (Python)
│   ├── src/main.py
│   ├── src/key_pool.py
│   ├── tests/
│   ├── AUDIT_REPORT_2026-07-23.md
│   └── README.md
│
├── nvidia/                 # DEPRECATED (Node.js reference only)
│   └── src/index.js
│
├── nous/                   # wrapper-nous (Python)
│   ├── wrapper_nous.py
│   ├── AUDIT_*.md
│   └── README.md
│
├── opencode/               # wrapper-opencode (OpenCode specialized)
│   ├── src/main.py
│   ├── src/key_pool.py
│   ├── tests/
│   ├── README.md
│   └── wrapper-opencode.service
│
└── dashboard.html
```

## wrapper-nvidia (NVIDIA NIM)

**Recommended implementation:** `nvidia-python/`

- Full OpenAI + Anthropic + Responses compatibility
- Multi-key rotation, pacing, load shedding (`INFLIGHT_SOFT_CAP=100`)
- Reasoning injection, aliases (Claude Code), model verification
- Production hardening: anti-silence (960s), TTFT, pre-response, stream buffering
- .env hot reload + rich metrics

**Migrate to this version.** The old Node.js implementation (`nvidia/`) will be removed.

See:
- `nvidia-python/README.md`
- `nvidia-python/AUDIT_REPORT_2026-07-23.md` (full 100/100 audit)

## wrapper-nous

Full-featured proxy for `inference-api.nousresearch.com`.

- 100/100 OpenAI + Anthropic + Responses + parallel tools
- SSE heartbeat, vision, thinking, rich metadata
- See `nous/README.md` and `nous/FINAL_100_AUDIT.md`

## Quick Start (Recommended)

### NVIDIA (new canonical)

```bash
cd nvidia-python
pip install -r requirements.txt
cp .env.example .env   # add your NVIDIA_API_KEY_*
python -m uvicorn src.main:app --port 9101
```

### Nous

```bash
cd nous
pip install -r requirements.txt
python -m uvicorn wrapper_nous:app --port 9106
```

## Migration Notice

- `~/wrappers/nvidia` (Node.js) → **deprecated**. Point all clients and services to `nvidia-python`.
- Both `nvidia-python` and `nous` are maintained at **100/100 production grade**.

## License

Internal use only.
## wrapper-opencode

Specialized OpenCode proxy (modeled after nvidia-python).

- OpenAI Chat + Responses + Anthropic compatible
- Multi-key rotation + load shedding (`INFLIGHT_SOFT_CAP=100`)
- Production streaming + heartbeat
- See `opencode/README.md`

Quick start:
```bash
cd opencode
pip install -r requirements.txt
cp .env.example .env   # add OPENCODE_API_KEY_*
python -m uvicorn src.main:app --port 9107
```
