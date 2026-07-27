# Fix OPENCODE - implement SEMUA temuan, minimal-diff
Scope: HANYA /root/wrapper/opencode/. JANGAN: restart service, run server, git commit/push, edit di luar opencode/, sentuh opencode/.env (boleh opencode/.env.example).
Baca dulu suggested-fix di:
- audit_report/parts/2026-07-27_part_opencode_blackbox_common_registry.md (section 1 OC-1..OC-18, section 5 drift DR-*, section 6)
- audit_report/parts/2026-07-27_part_transparency.md (section 3 opencode O1-O31)
- audit_report/parts/2026-07-27_part_latency.md (F3, F5)
- audit_report/parts/2026-07-27_part_infra_runtime.md (F6 JSONDecodeError - opencode juga kena)

## A. Stabilitas/Keamanan
1. OC-1 CRITICAL: ganti Mutex hand-rolled di src/key_pool.py dengan asyncio.Lock.
2. OC
