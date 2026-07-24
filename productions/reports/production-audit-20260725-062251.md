# Production audit report

- Timestamp: `2026-07-25T06:20:45.588301+07:00`
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
- **PASS** — endpoint registry: HTTP 200, 5.6 ms, status=ok
- **PASS** — endpoint nvidia: HTTP 200, 4.2 ms, status=ok
- **PASS** — endpoint nous: HTTP 200, 1.5 ms, status=ok
- **PASS** — endpoint opencode: HTTP 200, 1.4 ms, status=ok
- **PASS** — endpoint blackbox: HTTP 200, 1.8 ms, status=ok
- **PASS** — repository tests: ........................................................................ [100%]
72 passed in 6.12s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=469.4 p95=4452.6 p99=5422.8 mean=978.8
ttft_ms p50=340.3 p95=729.3 p99=4409.9

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=445.7 p95=1748.8 p99=3592.5 mean=546.9
ttft_ms p50=322.1 p95=473.2 p99=488.2

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=452.4 p95=3621.1 p99=4953.5 mean=837.8
ttft_ms p50=331.2 p95=553.7 p99=1480.5

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=473.3 p95=13476.0 p99=14499.9 mean=2105.2
ttft_ms p50=395.1 p95=9527.5 p99=10844.8

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=578.0 p95=2525.3 p99=6756.7 mean=973.6
ttft_ms p50=573.5 p95=2419.0 p99=2493.2

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=446.0 p95=3744.4 p99=5020.8 mean=788.2
ttft_ms p50=321.0 p95=687.0 p99=2178.9

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=439.2 p95=5297.1 p99=6412.2 mean=944.1
ttft_ms p50=345.4 p95=3431.4 p99=4137.2

- **PASS** — bounded load: ok=50/50 error=0
latency_ms p50=541.3 p95=2438.0 p99=4019.1 mean=842.9
ttft_ms p50=395.7 p95=3465.8 p99=3983.7


## Summary
- PASS: `42`
- FAIL: `0`
- BLOCKED: `0`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.
- The report intentionally does not include secrets or response bodies.
