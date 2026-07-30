# Production audit report

- Timestamp: `2026-07-25T06:40:26.648262+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 6.3 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.4 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.6 ms, status=ok
- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 647.0 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **PASS** — exact-model smoke [responses]: wrapper=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=responses, api_key_env=WRAPPER_API_KEY, HTTP 200, 2145.6 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1984.4 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1847.8 ms, returned_model=poolside/laguna-s-2.1:free, error_type=message,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **BLOCKED** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.7 ms, returned_model=not-present, error_type=server_error,error_code=none, external_outage=opencode.ai
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **BLOCKED** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 503, 1.8 ms, returned_model=not-present, error_type=api_error,error_code=none, external_outage=opencode.ai
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **PASS** — exact-model smoke [chat]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=chat, api_key_env=WRAPPER_API_KEY, HTTP 200, 1523.3 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=unknown,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required
- **PASS** — exact-model smoke [messages]: wrapper=http://127.0.0.1:9104/v1, model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, surface=messages, api_key_env=WRAPPER_API_KEY, HTTP 200, 1395.1 ms, returned_model=blackboxai/nvidia/nemotron-nano-12b-v2-vl, error_type=message,error_code=none
- **BLOCKED** — explicit model test: --wrapper-url and --model are required

## Summary
- PASS: `37`
- FAIL: `0`
- BLOCKED: `11`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
