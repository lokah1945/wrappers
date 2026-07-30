# Production audit report

- Timestamp: `2026-07-25T07:46:48.841973+07:00`
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
- **PASS** — endpoint nvidia: HTTP 200, 4.3 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.4 ms, status=degraded
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.07s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 729.7 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=597.2 p95=8226.8 p99=11969.4 mean=1882.4
ttft_ms p50=485.2 p95=3881.4 p99=5424.4

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 481.4 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=456.7 p95=655.4 p99=755.8 mean=397.5
ttft_ms p50=393.7 p95=477.5 p99=486.2

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 2100.6 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: ok=40/40 error=0
latency_ms p50=2027.2 p95=7209.6 p99=8039.2 mean=2613.1
ttft_ms p50=975.7 p95=1916.9 p99=1969.3

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1074.1 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: ok=40/40 error=0
latency_ms p50=1676.9 p95=6874.8 p99=9503.8 mean=2367.5
ttft_ms p50=2176.2 p95=6065.8 p99=7025.9

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1206.0 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1308.9 p95=3125.0 p99=4768.9 mean=1279.3
ttft_ms p50=951.8 p95=1542.8 p99=2671.0

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 736.1 ms, returned_model=deepseek-v4-flash-free, error_type=message,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1100.4 p95=2703.1 p99=3498.1 mean=1057.4
ttft_ms p50=924.0 p95=1660.0 p99=3275.6

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1543.6 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: ok=40/40 error=0
latency_ms p50=1047.7 p95=1558.7 p99=11724.7 mean=1371.7
ttft_ms p50=1060.3 p95=1450.1 p99=1479.7

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1199.1 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=message,error_code=none
- **FAIL** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: load reliability error (1/40): ok=39/40 error=1
latency_ms p50=794.3 p95=1306.1 p99=6359.5 mean=759.4
ttft_ms p50=802.8 p95=5554.4 p99=6293.5
error_class=provider_error external=0 other=1 sample_status=500 sample_err=


## Summary
- PASS: `49`
- FAIL: `1`
- BLOCKED: `0`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
