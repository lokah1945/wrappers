# Production audit report

- Timestamp: `2026-07-25T02:45:12.820242+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit: 6b5a1d9fb18e51215c712a555d7f7a29543704ee
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
- **FAIL** — systemd wrapper-model-registry.service: inactive
- **BLOCKED** — systemd wrapper-nvidia-python.service: inactive
- **FAIL** — systemd wrapper-nous.service: inactive
- **FAIL** — systemd wrapper-opencode.service: inactive
- **FAIL** — systemd wrapper-blackbox.service: inactive
- **PASS** — endpoint registry: HTTP 200, 12.1 ms, status=ok
- **FAIL** — orphan runtime registry: endpoint is healthy but its systemd unit is not active
- **PASS** — endpoint nvidia: HTTP 200, 7.8 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **FAIL** — orphan runtime nous: endpoint is healthy but its systemd unit is not active
- **PASS** — endpoint opencode: HTTP 200, 1.6 ms, status=ok
- **FAIL** — orphan runtime opencode: endpoint is healthy but its systemd unit is not active
- **PASS** — endpoint blackbox: HTTP 200, 2.0 ms, status=ok
- **FAIL** — orphan runtime blackbox: endpoint is healthy but its systemd unit is not active
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 5.94s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9101/v1, model=nvidia/llama-3.3-nemotron-super-49b-v1, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 591.2 ms, returned_model=nvidia/llama-3.3-nemotron-super-49b-v1, error_type=unknown,error_code=none

## Summary
- PASS: `25`
- FAIL: `8`
- BLOCKED: `2`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
