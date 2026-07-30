# Production audit report

- Timestamp: `2026-07-25T06:44:08.608677+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 6.2 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.5 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.8 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 3.1 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.5 ms, status=degraded
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.77s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 642.7 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=471.9 p95=3052.2 p99=4213.7 mean=771.2
ttft_ms p50=458.1 p95=692.7 p99=1685.9

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 473.9 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=480.3 p95=16231.4 p99=17582.2 mean=2311.6
ttft_ms p50=349.7 p95=553.4 p99=2483.1

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1904.9 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=932.5 p95=3292.9 p99=4037.9 mean=1211.4
ttft_ms p50=706.7 p95=1871.5 p99=1898.7

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 646.3 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=1330.7 p95=2973.7 p99=4347.9 mean=1431.3
ttft_ms p50=1199.9 p95=2391.7 p99=2482.3

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1562.0 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=21/40 error=19
latency_ms p50=6.2 p95=4778.5 p99=14642.6 mean=1621.4
ttft_ms p50=1.2 p95=1520.0 p99=4510.0
sample_error= (429, '{"error":{"message":"All configured OpenCode keys failed or are rate-limited. Last error: Rate limit exceeded. Please try again later.","type":"rate_limit_error","code":429}}')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 2.7 ms, returned_model=not-present, error_type=api_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=7/40 error=33
latency_ms p50=3.5 p95=10.3 p99=10.3 mean=5.3
ttft_ms p50=3.6 p95=10.4 p99=10.5
sample_error= (503, '')

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1592.7 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=39/40 error=1
latency_ms p50=964.2 p95=6505.6 p99=6945.4 mean=1496.1
ttft_ms p50=1393.4 p95=6859.3 p99=6859.3
sample_error= (500, '{"error":{"message":"blackbox.Error: InternalServerError: Vercel_ai_gatewayException - Connection error.. Received Model Group=blackboxai/nvidia/nemotron-nano-12b-v2-vl\\nAvailable Model Group Fallbacks=None","type":"server_error","code":500}}')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.9 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=7/40 error=33
latency_ms p50=3.8 p95=53.9 p99=53.9 mean=17.5
ttft_ms p50=4.5 p95=33.8 p99=54.0
sample_error= (503, '{"error":{"message":"No capacity","type":"server_error"}}')


## Summary
- PASS: `47`
- FAIL: `0`
- BLOCKED: `3`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
