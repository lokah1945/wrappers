# Migration Notice: Node.js → Python

## Status: In Progress (Active Migration)

The `wrapper-nvidia` service is being migrated from Node.js to Python.

### Current State

| Component | Status | Port | Directory |
|-----------|--------|------|-----------|
| Node.js (legacy) | **Deprecated** | 9100 | `/root/wrapper/nvidia` |
| Python (new) | **Active** | 9101 | `/root/wrapper/nvidia-python` |

### What's Been Migrated

- ✅ `src/capabilities.py` — Full parity with `capabilities.js`
- ✅ `src/key_pool.py` — Full parity with `key_pool.js`
- ✅ `src/metrics.py` — Full parity with `metrics.js`
- ✅ `src/registry.py` — Full parity with `registry.js`
- ✅ `src/anthropic_compat.py` — Full parity with `anthropic_compat.js`
- ✅ `src/responses_compat.py` — Full parity with `responses_compat.js`
- ✅ `src/alert_history.py` — Full parity with `alert_history.js`
- ✅ `src/loki_push.py` — Full parity with `loki_push.js`
- ✅ `src/main.py` — FastAPI server with all routes from `index.js`
- ✅ Tests — 118 tests passing

### What's Next

- [ ] End-to-end integration testing with real NVIDIA NIM API
- [ ] Performance benchmarking vs Node.js version
- [ ] Gradual traffic migration (canary deployment)
- [ ] Deprecation timeline for Node.js version

### Running the Python Version

```bash
cd /root/wrapper/nvidia-python
python3 -m uvicorn src.main:app --host 0.0.0.0 --port 9101
```

### Configuration

The Python version uses the same `.env` configuration as Node.js, with the following key differences:
- `LISTEN_PORT=9101` (Node.js uses 9100)
- Same NVIDIA API keys, rate limits, and all other settings

### Feature Parity

The Python version provides **full feature parity** with the Node.js version:
- Key rotation and rate limiting
- Capability-aware fallback cascade
- OpenAI Responses API support
- Anthropic Messages API compatibility
- Prometheus metrics
- Dashboard
- SSE real-time events
- Bearer token authentication
- Deprecated model redirect handling
- Claude Code alias mapping
