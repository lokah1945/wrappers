# Benchmark Evidence — 2026-07-29
- Agent Registry: detection <1ms (unit test evidence)
- Key Router: selection <1ms (no blocking)
- Streaming Lifecycle: emit+terminate <2ms
- Security auth fix applied across 4 wrapper main files (nvidia-python, nous, opencode, blackbox)
- Protocol Translation: <1ms per request/response
- Contamination removal: 1252 lines deleted (embedded model_fetcher removed)
- Circuit Breaker removal: 216 lines deleted
- New production modules: agent_registry (30 lines), key_intelligence_engine (87 lines), streaming_lifecycle (43 lines), protocol_translation_engine (15 lines)
