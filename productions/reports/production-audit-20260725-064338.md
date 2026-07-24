# Production audit report

- Timestamp: `2026-07-25T06:41:18.848183+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — runtime commit model-registry: runtime=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc repository=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc
- **PASS** — runtime commit nvidia: runtime=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc repository=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc
- **PASS** — runtime commit nous: runtime=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc repository=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc
- **PASS** — runtime commit opencode: runtime=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc repository=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc
- **PASS** — runtime commit blackbox: runtime=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc repository=5dde2425c6db8dbcf7ceeedcecefcc5d9e01fecc
- **PASS** — git branch: main
- **PASS** — git origin: configured
- **BLOCKED** — working tree: uncommitted changes are present
- **PASS** — installer executable: /root/wrapper/install.sh
- **PASS** — directory common: /root/wrapper/common
- **PASS** — directory model-registry: /root/wrapper/model-registry
- **PASS** — directory nvidia-python: /root/wrapper/nvidia-python
- **PASS** — directory nous: /root/wrapper/nous
- **PASS** — directory opencode: /root/wrapper/opencode
- **PASS** — directory blackbox: /root/wrapper/blackbox
- **PASS** — no model substitution markers: none
- **PASS** — config presence nvidia-python/.env: present
- **PASS** — config presence nous/.env: present
- **PASS** — config presence opencode/.env: present
- **PASS** — config presence blackbox/.env: present
- **PASS** — config presence model-registry/.env: present
- **PASS** — systemd wrapper-model-registry.service: active
- **PASS** — systemd wrapper-nvidia-python.service: active
- **PASS** — systemd wrapper-nous.service: active
- **PASS** — systemd wrapper-opencode.service: active
- **PASS** — systemd wrapper-blackbox.service: active
- **PASS** — endpoint registry: HTTP 200, 5.2 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 6.2 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 3.9 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=degraded
- **PASS** — endpoint blackbox: HTTP 200, 1.9 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.73s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 784.8 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=515.1 p95=1983.1 p99=3374.9 mean=670.5
ttft_ms p50=386.1 p95=736.1 p99=794.6

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 476.1 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=452.3 p95=3768.9 p99=4595.6 mean=933.8
ttft_ms p50=330.2 p95=561.0 p99=3457.9

- **FAIL** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 503, 462.8 ms, returned_model=not-present, error_type=server_error,error_code=503
- **PASS** — bounded load: ok=12/40 error=28
latency_ms p50=907.7 p95=1786.7 p99=1843.3 mean=837.4
ttft_ms p50=1497.0 p95=1786.7 p99=2168.4
sample_error= (503, '')

- **FAIL** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 447.7 ms, returned_model=not-present, error_type=server_error,error_code=none
- **PASS** — bounded load: ok=11/40 error=29
latency_ms p50=710.1 p95=2020.9 p99=2020.9 mean=586.8
ttft_ms p50=707.7 p95=945.3 p99=1676.5
sample_error= (503, '{"error":{"message":"The requested model is temporarily unavailable due to upstream capacity limits. Please try again in a moment.","type":"server_error","code":503}}')

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1514.1 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=19/40 error=21
latency_ms p50=11.7 p95=1547.6 p99=1734.3 mean=595.5
ttft_ms p50=2.3 p95=1082.9 p99=1131.3
sample_error= (429, '{"type":"error","error":{"type":"api_error","message":"{\'error\': {\'message\': \'All configured OpenCode keys failed or are rate-limited. Last error: Rate limit exceeded. Please try again later.\', \'type\': \'rate_limit_error\', \'code\': 429}}"}}')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.8 ms, returned_model=not-present, error_type=api_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=10/40 error=30
latency_ms p50=5.6 p95=22.0 p99=22.0 mean=9.6
ttft_ms p50=4.7 p95=12.8 p99=13.1
sample_error= (503, '')

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1583.4 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=39/40 error=1
latency_ms p50=1260.0 p95=6318.2 p99=7082.4 mean=1472.1
ttft_ms p50=1487.0 p95=6310.8 p99=7018.5
sample_error= (500, '')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.9 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=6/40 error=34
latency_ms p50=7.8 p95=19.5 p99=19.5 mean=12.3
ttft_ms p50=3.9 p95=6.5 p99=19.5
sample_error= (503, '')


## Summary
- PASS: `45`
- FAIL: `2`
- BLOCKED: `3`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
