# Production audit report

- Timestamp: `2026-07-25T03:08:22.299840+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit (marker): 62307eb608e74254ea31faba92088885e7ad9787
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
- **PASS** — endpoint registry: HTTP 200, 5.6 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 3.8 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.6 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.8 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.54s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 1465.1 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none
- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9102/v1, model=poolside/laguna-s-2.1:free, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 2791.7 ms, returned_model=poolside/laguna-s-2.1:free, error_type=unknown,error_code=none
- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 1865.0 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none
- **FAIL** — exact-model smoke: wrapper_url=http://127.0.0.1:9104/v1, model=sonnet, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 1211.4 ms, returned_model=blackboxai/nvidia/nemotron-3-super-120b-a12b:free, error_type=unknown,error_code=none

## Summary
- PASS: `32`
- FAIL: `1`
- BLOCKED: `1`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
