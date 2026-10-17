# Production audit report

- Timestamp: `2026-10-17T17:33:35.382454+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PENDING** — provider opencode: frozen by wrappers.json; implementation, configuration, service, logs and endpoints excluded
- **PENDING** — provider blackbox: frozen by wrappers.json; implementation, configuration, service, logs and endpoints excluded
- **PASS** — repository layout: wrappers.json present
- **PASS** — checkout commit: 6c80e12569b57ca07b3b392c24246ce4721034ae
- **PASS** — git branch: main
- **PASS** — git origin: configured
- **PASS** — working tree: clean
- **PASS** — installer executable: /root/wrapper/install.sh
- **PASS** — directory common: /root/wrapper/common
- **PASS** — directory nvidia-python: /root/wrapper/nvidia-python
- **PASS** — directory nous: /root/wrapper/nous
- **PASS** — directory openrouter: /root/wrapper/openrouter
- **PASS** — directory model-registry: /root/wrapper/model-registry
- **PASS** — no model substitution markers: none
- **PASS** — config presence nvidia-python/.env: present
- **PASS** — config presence nous/.env: present
- **PASS** — config presence openrouter/.env: present
- **PASS** — config presence model-registry/.env: present
- **PASS** — systemd wrapper-nvidia-python.service: active
- **PASS** — systemd wrapper-nous.service: active
- **PASS** — systemd wrapper-openrouter.service: active
- **PASS** — systemd wrapper-model-registry.service: active
- **PASS** — endpoint nvidia-python: HTTP 200, 11.0 ms, status=ok
- **PASS** — runtime commit nvidia-python: runtime=6c80e12569b57ca07b3b392c24246ce4721034ae deployed=6c80e12569b57ca07b3b392c24246ce4721034ae
- **PASS** — readiness nvidia-python: HTTP 200, 3.9 ms, ready=True
- **PASS** — endpoint nous: HTTP 200, 6.5 ms, status=ok
- **PASS** — runtime commit nous: runtime=6c80e12569b57ca07b3b392c24246ce4721034ae deployed=6c80e12569b57ca07b3b392c24246ce4721034ae
- **PASS** — readiness nous: HTTP 200, 29.7 ms, ready=True
- **PASS** — endpoint openrouter: HTTP 200, 4.2 ms, status=ok
- **PASS** — runtime commit openrouter: runtime=6c80e12569b57ca07b3b392c24246ce4721034ae deployed=6c80e12569b57ca07b3b392c24246ce4721034ae
- **PASS** — readiness openrouter: HTTP 200, 2.4 ms, ready=True
- **PASS** — endpoint model-registry: HTTP 200, 2.2 ms, status=ok
- **PASS** — runtime commit model-registry: runtime=6c80e12569b57ca07b3b392c24246ce4721034ae deployed=6c80e12569b57ca07b3b392c24246ce4721034ae

## Summary
- PASS: `31`
- FAIL: `0`
- BLOCKED: `0`

## Interpretation
- PENDING providers are frozen and excluded from production judgment; they are never inspected or probed.
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
