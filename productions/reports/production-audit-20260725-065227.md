# Production audit report

- Timestamp: `2026-07-25T06:50:18.738893+07:00`
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
- **PASS** — working tree: clean
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
- **PASS** — endpoint registry: HTTP 200, 5.9 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.5 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.9 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 3.6 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.9 ms, status=degraded
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.69s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 820.5 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=716.7 p95=4931.3 p99=5282.2 mean=1421.4
ttft_ms p50=434.2 p95=1297.8 p99=2223.4

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 506.2 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=467.8 p95=688.6 p99=932.7 mean=434.6
ttft_ms p50=398.6 p95=577.0 p99=900.2

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 3414.7 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=993.9 p95=2943.0 p99=8086.1 mean=1387.3
ttft_ms p50=1262.3 p95=2488.8 p99=2684.8

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 926.9 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=1170.4 p95=3906.5 p99=6214.1 mean=1500.5
ttft_ms p50=1652.4 p95=3532.6 p99=6101.3

- **BLOCKED** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.8 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=11/40 error=29
latency_ms p50=9.1 p95=14.1 p99=14.1 mean=9.1
ttft_ms p50=4.2 p95=9.8 p99=9.8
sample_error= (503, '')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.8 ms, returned_model=not-present, error_type=api_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=9/40 error=31
latency_ms p50=7.1 p95=13.6 p99=13.6 mean=8.5
ttft_ms p50=5.7 p95=12.6 p99=12.6
sample_error= (503, '')

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 7110.8 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=1311.2 p95=6864.7 p99=8453.4 mean=1670.8
ttft_ms p50=1382.7 p95=2863.1 p99=5952.2

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1526.5 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=message,error_code=none
- **PASS** — bounded load: ok=23/40 error=17
latency_ms p50=1044.5 p95=1791.9 p99=2086.8 mean=757.1
ttft_ms p50=985.9 p95=5024.7 p99=5524.5
sample_error= (500, '')


## Summary
- PASS: `48`
- FAIL: `0`
- BLOCKED: `2`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
