# Production audit report

- Timestamp: `2026-07-25T06:18:01.398834+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 5.1 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.0 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.8 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 6.1 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.5 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.70s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=439.6 p95=1109.4 p99=4087.2 mean=527.7
ttft_ms p50=345.1 p95=646.5 p99=680.8

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=566.5 p95=3880.2 p99=4784.6 mean=1122.4
ttft_ms p50=350.6 p95=1354.0 p99=2926.7

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=452.7 p95=5193.1 p99=5750.3 mean=1269.6
ttft_ms p50=325.0 p95=3965.1 p99=4648.5

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=451.4 p95=1452.7 p99=2995.0 mean=520.7
ttft_ms p50=333.5 p95=634.6 p99=696.7

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=482.0 p95=9941.2 p99=10034.6 mean=1427.3
ttft_ms p50=377.7 p95=2609.1 p99=9743.0

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=451.4 p95=4531.6 p99=5429.9 mean=810.4
ttft_ms p50=334.5 p95=551.1 p99=3784.0

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=398.5 p95=627.8 p99=794.1 mean=362.0
ttft_ms p50=348.5 p95=510.6 p99=545.2

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=461.3 p95=6098.7 p99=8083.2 mean=1186.7
ttft_ms p50=331.0 p95=2855.7 p99=3418.3


## Summary
- PASS: `41`
- FAIL: `0`
- BLOCKED: `1`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
