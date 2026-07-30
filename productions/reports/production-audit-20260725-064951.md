# Production audit report

- Timestamp: `2026-07-25T06:47:44.628337+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 4.8 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 6.7 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 2.2 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.6 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.6 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.07s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 694.9 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=465.1 p95=6956.1 p99=9362.2 mean=1506.0
ttft_ms p50=326.5 p95=510.9 p99=794.8

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 448.6 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=447.1 p95=2046.7 p99=3460.1 mean=604.5
ttft_ms p50=385.5 p95=2003.4 p99=3370.8

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 2160.2 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=916.1 p95=4875.2 p99=5592.5 mean=1436.0
ttft_ms p50=601.9 p95=1843.2 p99=4467.5

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1801.6 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **PASS** — bounded load: ok=40/40 error=0
latency_ms p50=1174.7 p95=2642.9 p99=3050.1 mean=1311.8
ttft_ms p50=946.4 p95=2386.3 p99=2964.4

- **BLOCKED** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.7 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=14/40 error=26
latency_ms p50=7.1 p95=11.3 p99=12.2 mean=7.5
ttft_ms p50=7.9 p95=12.2 p99=12.2
sample_error= (503, '{"type":"error","error":{"type":"api_error","message":"{\'error\': {\'message\': \'No capacity\', \'type\': \'server_error\'}}"}}')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.7 ms, returned_model=not-present, error_type=api_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=6/40 error=34
latency_ms p50=4.1 p95=10.9 p99=10.9 mean=5.5
ttft_ms p50=4.0 p95=8.7 p99=9.1
sample_error= (503, '{"type":"error","error":{"type":"api_error","message":"{\'error\': {\'message\': \'No capacity\', \'type\': \'server_error\'}}"}}')

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1605.2 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load: ok=37/40 error=3
latency_ms p50=960.4 p95=6087.2 p99=6424.2 mean=1350.0
ttft_ms p50=1294.1 p95=5507.9 p99=6079.3
sample_error= (500, '{"type":"error","error":{"type":"server_error","message":"blackbox.Error: InternalServerError: Vercel_ai_gatewayException - Connection error.. Received Model Group=blackboxai/nvidia/nemotron-nano-12b-v2-vl\\nAvailable Model Group Fallbacks=None"}}')

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.9 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **PASS** — bounded load: ok=10/40 error=30
latency_ms p50=20.3 p95=69.0 p99=69.0 mean=29.1
ttft_ms p50=20.3 p95=33.8 p99=33.8
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
