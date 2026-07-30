# Production audit report

- Timestamp: `2026-07-25T07:44:26.990047+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 4.9 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.4 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.6 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.6 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.21s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 589.1 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=467.9 p95=911.6 p99=4634.0 mean=513.0
ttft_ms p50=419.8 p95=634.0 p99=878.1

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 345.9 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=475.2 p95=1315.4 p99=1356.8 mean=473.6
ttft_ms p50=358.7 p95=547.6 p99=1284.8

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1042.0 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: ok=40/40 error=0
latency_ms p50=1588.2 p95=6608.1 p99=10436.9 mean=2349.8
ttft_ms p50=1998.1 p95=5931.8 p99=8032.1

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 3994.0 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **FAIL** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: load reliability error (20/40): ok=20/40 error=20
latency_ms p50=170.8 p95=3005.1 p99=4367.5 mean=1120.1
ttft_ms p50=976.7 p95=3078.7 p99=3311.0
error_class=provider_error external=4 other=16 sample_status=503 sample_err=

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 4870.2 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1064.4 p95=3240.3 p99=5832.4 mean=1076.3
ttft_ms p50=942.1 p95=3630.8 p99=5766.0

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 7348.6 ms, returned_model=deepseek-v4-flash-free, error_type=message,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1019.5 p95=2332.7 p99=5437.2 mean=977.3
ttft_ms p50=958.1 p95=1083.1 p99=2729.2

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1529.4 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **FAIL** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: load reliability error (2/40): ok=38/40 error=2
latency_ms p50=941.5 p95=3066.8 p99=10122.5 mean=1333.5
ttft_ms p50=891.6 p95=3001.5 p99=7635.6
error_class=provider_error external=0 other=2 sample_status=500 sample_err={"error":{"message":"blackbox.Error: InternalServerError: Vercel_ai_gatewayException - Connection error.. Received Model Group=blackboxai/nvidia/nemotron-nano-12b-v2-vl\nAvailable Model Group Fallback

- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.8 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **BLOCKED** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: external provider outage (32/40 errored): ok=8/40 error=32
latency_ms p50=19.9 p95=21.0 p99=21.0 mean=13.1
ttft_ms p50=9.1 p95=21.0 p99=21.0
error_class=external_outage external=32 other=0 sample_status=503 sample_err={"type":"error","error":{"type":"server_error","message":"No capacity"}}


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
