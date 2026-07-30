# Production audit report

- Timestamp: `2026-07-25T09:29:45.949924+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 5.8 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.7 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.4 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.6 ms, status=degraded
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.23s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1160.5 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=621.0 p95=15597.9 p99=17359.5 mean=2877.1
ttft_ms p50=370.8 p95=6024.2 p99=16526.3

- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 1719.7 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9101/v1 nvidia/llama-3.3-nemotron-super-49b-v1]: ok=40/40 error=0
latency_ms p50=613.3 p95=8971.4 p99=10342.6 mean=2243.9
ttft_ms p50=324.3 p95=1070.3 p99=1722.3

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1090.4 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: ok=40/40 error=0
latency_ms p50=1291.0 p95=3350.8 p99=4051.1 mean=1358.3
ttft_ms p50=1385.6 p95=2160.3 p99=2301.4

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 2901.3 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9102/v1 poolside/laguna-s-2.1:free]: ok=40/40 error=0
latency_ms p50=1859.5 p95=3195.7 p99=3819.9 mean=1721.3
ttft_ms p50=1920.6 p95=2538.7 p99=2623.0

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 2448.4 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1112.3 p95=5569.3 p99=8675.2 mean=1557.5
ttft_ms p50=834.3 p95=5096.9 p99=6487.8

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 674.9 ms, returned_model=deepseek-v4-flash-free, error_type=message,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9103/v1 deepseek-v4-flash-free]: ok=40/40 error=0
latency_ms p50=1294.0 p95=5656.3 p99=8486.7 mean=1684.3
ttft_ms p50=1185.9 p95=4215.3 p99=8250.2

- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1421.5 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **PASS** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: ok=40/40 error=0
latency_ms p50=1103.5 p95=7141.8 p99=7796.6 mean=1561.2
ttft_ms p50=834.7 p95=7076.2 p99=7094.8

- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 865.4 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=message,error_code=none
- **BLOCKED** — bounded load [http://127.0.0.1:9104/v1 blackboxai/nvidia/nemotron-nano-12b-v2-vl]: external provider outage (18/40 errored): ok=22/40 error=18
latency_ms p50=656.6 p95=1334.7 p99=1403.9 mean=508.7
ttft_ms p50=6.6 p95=1321.0 p99=5349.7
error_class=external_outage external=15 other=3 sample_status=500 sample_err={"type":"error","error":{"type":"server_error","message":"blackbox.Error: InternalServerError: Vercel_ai_gatewayException - Connection error.. Received Model Group=blackboxai/nvidia/nemotron-nano-12b-


## Summary
- PASS: `49`
- FAIL: `0`
- BLOCKED: `1`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
