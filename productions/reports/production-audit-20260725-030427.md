# Production audit report

- Timestamp: `2026-07-25T03:04:25.991672+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit (excl. report-only): c0a65354213e2d5302d0355dad5eb85213e98716
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
- **PASS** — endpoint registry: HTTP 200, 5.2 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.1 ms, status=ok
- **FAIL** — runtime commit nvidia: runtime=62307eb608e74254ea31faba92088885e7ad9787 repository=c0a65354213e2d5302d0355dad5eb85213e98716
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **FAIL** — runtime commit nous: runtime=62307eb608e74254ea31faba92088885e7ad9787 repository=c0a65354213e2d5302d0355dad5eb85213e98716
- **PASS** — endpoint opencode: HTTP 200, 1.5 ms, status=ok
- **FAIL** — runtime commit opencode: runtime=62307eb608e74254ea31faba92088885e7ad9787 repository=c0a65354213e2d5302d0355dad5eb85213e98716
- **PASS** — endpoint blackbox: HTTP 200, 1.9 ms, status=ok
- **FAIL** — runtime commit blackbox: runtime=62307eb608e74254ea31faba92088885e7ad9787 repository=c0a65354213e2d5302d0355dad5eb85213e98716
- **PASS** — exact-model smoke: wrapper_url=http://127.0.0.1:9103/v1, model=deepseek-v4-flash-free, surface=chat_completions, api_key_env=WRAPPER_API_KEY, HTTP 200, 1896.2 ms, returned_model=deepseek-v4-flash-free, error_type=unknown,error_code=none

## Summary
- PASS: `28`
- FAIL: `4`
- BLOCKED: `1`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
