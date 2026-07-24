# Production audit report

- Timestamp: `2026-07-25T02:50:23.523769+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit: 10ea258bc2ab258192394afd8000a64227b05bd8
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
- **PASS** — endpoint registry: HTTP 200, 5.1 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.2 ms, status=ok
- **FAIL** — runtime commit nvidia: runtime=ce3f0b7dd8b68b36f6b2d8765232b60155d7c420 repository=10ea258bc2ab258192394afd8000a64227b05bd8
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **FAIL** — runtime commit nous: runtime=ce3f0b7dd8b68b36f6b2d8765232b60155d7c420 repository=10ea258bc2ab258192394afd8000a64227b05bd8
- **PASS** — endpoint opencode: HTTP 200, 1.9 ms, status=ok
- **FAIL** — runtime commit opencode: runtime=ce3f0b7dd8b68b36f6b2d8765232b60155d7c420 repository=10ea258bc2ab258192394afd8000a64227b05bd8
- **PASS** — endpoint blackbox: HTTP 200, 1.8 ms, status=ok
- **FAIL** — runtime commit blackbox: runtime=ce3f0b7dd8b68b36f6b2d8765232b60155d7c420 repository=10ea258bc2ab258192394afd8000a64227b05bd8
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.50s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 541.8 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none

## Summary
- PASS: `31`
- FAIL: `4`
- BLOCKED: `0`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
