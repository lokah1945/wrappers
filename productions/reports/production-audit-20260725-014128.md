# Production audit report

- Timestamp: `2026-07-25T01:41:28.568916+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit: 7cbda8acc6efdad1c980dec94e90abaadd62aa38
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
- **BLOCKED** — systemd wrapper-model-registry.service: inactive
- **BLOCKED** — systemd wrapper-nvidia-python.service: inactive
- **BLOCKED** — systemd wrapper-nous.service: inactive
- **BLOCKED** — systemd wrapper-opencode.service: inactive
- **BLOCKED** — systemd wrapper-blackbox.service: inactive
- **PASS** — endpoint registry: HTTP 200, 7.4 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 3.8 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.6 ms, status=ok
- **BLOCKED** — endpoint blackbox: HTTP 0, 0.2 ms, <urlopen error [Errno 111] Connection refused>

## Summary
- PASS: `19`
- FAIL: `0`
- BLOCKED: `7`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
