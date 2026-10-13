# Production audit report

- Timestamp: `2026-10-07T04:12:28.561965+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — checkout commit: 1825f876d30feca307ae1198be8e832073b78015
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
- **PASS** — directory openrouter: /root/wrapper/openrouter
- **PASS** — no model substitution markers: none
- **PASS** — config presence model-registry/.env: present
- **PASS** — config presence nvidia-python/.env: present
- **PASS** — config presence nous/.env: present
- **PASS** — config presence opencode/.env: present
- **PASS** — config presence blackbox/.env: present
- **PASS** — config presence openrouter/.env: present
- **PASS** — systemd wrapper-model-registry.service: active
- **PASS** — systemd wrapper-nvidia-python.service: active
- **PASS** — systemd wrapper-nous.service: active
- **PASS** — systemd wrapper-opencode.service: active
- **PASS** — systemd wrapper-blackbox.service: active
- **PASS** — systemd wrapper-openrouter.service: active
- **PASS** — endpoint model-registry: HTTP 200, 5.0 ms, status=ok
- **PASS** — runtime commit model-registry: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **PASS** — endpoint nvidia-python: HTTP 200, 6.2 ms, status=ok
- **PASS** — runtime commit nvidia-python: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **PASS** — readiness nvidia-python: HTTP 200, 2.2 ms, ready=True
- **PASS** — endpoint nous: HTTP 200, 3.4 ms, status=ok
- **PASS** — runtime commit nous: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **PASS** — readiness nous: HTTP 200, 24.4 ms, ready=True
- **PASS** — endpoint opencode: HTTP 200, 6.4 ms, status=ok
- **PASS** — runtime commit opencode: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **FAIL** — readiness opencode: HTTP 200, 1888.2 ms, ready=False
- **PASS** — endpoint blackbox: HTTP 200, 9.3 ms, status=ok
- **PASS** — runtime commit blackbox: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **FAIL** — readiness blackbox: HTTP 200, 799.0 ms, ready=False
- **PASS** — endpoint openrouter: HTTP 200, 3.0 ms, status=ok
- **PASS** — runtime commit openrouter: runtime=1825f876d30feca307ae1198be8e832073b78015 deployed=1825f876d30feca307ae1198be8e832073b78015
- **PASS** — readiness openrouter: HTTP 200, 1.8 ms, ready=True

## Summary
- PASS: `40`
- FAIL: `2`
- BLOCKED: `1`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
