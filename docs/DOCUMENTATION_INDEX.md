# Documentation Index

**Last reorganized:** 2026-08-01

All documentation that is **not** required at runtime lives under `docs/`. The
repository root deliberately contains only three files that production depends
on or that a newcomer must read first:

| Root file | Why it stays at root |
|---|---|
| `README.md` | Entry point for the repo |
| `WRAPPER_CONTRACT.md` | The normative cross-wrapper contract — the spec every wrapper is held to |
| `wrappers.json` | Machine-readable deployment config (ports, entry points, capabilities) consumed by tooling |

---

## `docs/` — normative and process documents

| File | Purpose |
|---|---|
| [`CROSS_WRAPPER_BUG_POLICY.md`](CROSS_WRAPPER_BUG_POLICY.md) | **Normative.** A bug found in one wrapper must be checked and fixed in all five. |
| [`COMPATIBILITY_LAYER.md`](COMPATIBILITY_LAYER.md) | **Normative.** Operator-declared upstream dialect (1=OpenAI, 2=Anthropic, 3=Auto). |
| [`HOT_SWAP_RUNBOOK.md`](HOT_SWAP_RUNBOOK.md) | **Normative.** API-key hot swap: authoritative source, ownership rules, watcher behavior, reload bound, runbook scenarios. |
| [`MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md`](MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md) | The final-audit execution prompt (mission, gates G0–G10, hot-swap contract, refactor rules). |
| [`WRAPPERS_FINAL_AUDIT_BASELINE.md`](WRAPPERS_FINAL_AUDIT_BASELINE.md) | Pre-fix baseline evidence at `66ec1c1` (what this round started from). |
| [`TEMPLATE_WRAPPER.md`](TEMPLATE_WRAPPER.md) | Skeleton and checklist for adding a new provider wrapper. |
| [`ADR.md`](ADR.md) | Architecture decision records. |
| [`DOCUMENTATION_INDEX.md`](DOCUMENTATION_INDEX.md) | This file. |

## `docs/audits/` — audit reports (historical + current)

Point-in-time findings. **Superseded documents are retained for traceability,
not as statements of current state.** Several older reports assert
"100/100 / ZERO BUG"; those claims were contradicted by later audits and should
be read as historical artifacts only.

| Report | Date | Status |
|---|---|---|
| [`WRAPPERS_FINAL_AUDIT_2026-11-15.md`](audits/WRAPPERS_FINAL_AUDIT_2026-11-15.md) | 2026-11-15 | **Current.** Final audit execution: hot swap, native purity, coarena gates/deps; 260/260 acceptance, 40/40 hot swap, 24/24 diagnostics. |
| [`COARENA_CONTRACT_COMPLIANCE_2026-11-15.md`](audits/COARENA_CONTRACT_COMPLIANCE_2026-11-15.md) | 2026-11-15 | **Current.** Coarena WRAPPER_CONTRACT v3.2 compliance. |
| [`RUNTIME_AUDIT_2026-08-01.md`](audits/RUNTIME_AUDIT_2026-08-01.md) | 2026-08-01 | **Current.** Live agent-traffic verification; 8 runtime bugs (R-01…R-08) found and fixed. |
| [`FULL_MATRIX_AUDIT_2026-08-01.md`](audits/FULL_MATRIX_AUDIT_2026-08-01.md) | 2026-08-01 | **Current.** Full matrix audit — 240/240 checks, real SDK clients, 5 contract defects (F-1…F-5) fixed. |
| [`TRANSLATION_LAYER_AUDIT_2026-08-01.md`](audits/TRANSLATION_LAYER_AUDIT_2026-08-01.md) | 2026-08-01 | **Current.** AI Gateway translation-layer audit (F1-F7). |
| [`CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md`](audits/CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md) | 2026-08-01 | **Current.** SDK-compat / Codex (CODEX-RESP-02). |
| [`CODEX_RESP_REAUDIT_2026-08-01.md`](audits/CODEX_RESP_REAUDIT_2026-08-01.md) | 2026-08-01 | **Current.** Codex reasoning-only fix (CODEX-RESP-01). |
| [`BUG_ANALYSIS_2026-07-31.md`](audits/BUG_ANALYSIS_2026-07-31.md) | 2026-07-31 | **Current.** 37-finding deep audit across all wrappers. |
| [`FINAL_PRODUCTION_AUDIT_2026-07-29.md`](audits/FINAL_PRODUCTION_AUDIT_2026-07-29.md) | 2026-07-29 | Superseded |
| [`AUDIT_REVIEW_2026-07-29.md`](audits/AUDIT_REVIEW_2026-07-29.md) | 2026-07-29 | Superseded |
| [`AUDIT_NO_MODEL_FALLBACK_2026-07-29.md`](audits/AUDIT_NO_MODEL_FALLBACK_2026-07-29.md) | 2026-07-29 | Superseded |
| [`AUDIT_FINAL_100_PERFECT_2026-07-28.md`](audits/AUDIT_FINAL_100_PERFECT_2026-07-28.md) | 2026-07-28 | Superseded — claim not accurate |
| [`AUDIT_ZERO_TOLERANCE_2026-07-28.md`](audits/AUDIT_ZERO_TOLERANCE_2026-07-28.md) | 2026-07-28 | Superseded — claim not accurate |
| [`AUDIT_ZERO_BUG_2026-07-28.md`](audits/AUDIT_ZERO_BUG_2026-07-28.md) | 2026-07-28 | Superseded — claim not accurate |
| [`AUDIT_COMPREHENSIVE_2026-07-28_ROUND3.md`](audits/AUDIT_COMPREHENSIVE_2026-07-28_ROUND3.md) | 2026-07-28 | Superseded |
| [`AUDIT_FRESH_2026-07-28.md`](audits/AUDIT_FRESH_2026-07-28.md) | 2026-07-28 | Superseded |
| [`DEEP_AUDIT_SECURITY_2026-07-28.md`](audits/DEEP_AUDIT_SECURITY_2026-07-28.md) | 2026-07-28 | Superseded |
| [`DEEP_AUDIT_2026-07-26.md`](audits/DEEP_AUDIT_2026-07-26.md) | 2026-07-26 | Superseded |
| [`DEEP_AUDIT_BITLEVEL_2026-07-26.md`](audits/DEEP_AUDIT_BITLEVEL_2026-07-26.md) | 2026-07-26 | Superseded |
| [`AUDIT_PRODUCTION_2026-07-24.md`](audits/AUDIT_PRODUCTION_2026-07-24.md) | 2026-07-24 | Superseded |
| [`AUDIT_MODEL_AVAILABILITY_2026-07-24.md`](audits/AUDIT_MODEL_AVAILABILITY_2026-07-24.md) | 2026-07-24 | Superseded |

## `docs/reports/` — readiness, benchmarks, planning

| File | Purpose |
|---|---|
| [`RELEASE_GATES_2026-11-15.md`](reports/RELEASE_GATES_2026-11-15.md) | **Current.** Final-audit gate results G0–G10 with evidence links and honest exclusions. |
| [`RESOURCE_PROFILE_2026-11-15.md`](reports/RESOURCE_PROFILE_2026-11-15.md) | **Current.** Soak, key-churn bound, hot-swap overhead measurements and limits. |
| [`PRODUCTION_READINESS_2026-08-01.md`](reports/PRODUCTION_READINESS_2026-08-01.md) | Readiness scorecard and remaining gaps (pre final-audit; see RELEASE_GATES_2026-11-15 for current gate state). |
| [`WRAPPER_STANDARDIZATION_REPORT.md`](reports/WRAPPER_STANDARDIZATION_REPORT.md) | How the wrappers were standardized onto one structure. |
| [`PRODUCTION_READINESS_REPORT_2026-07-28.md`](reports/PRODUCTION_READINESS_REPORT_2026-07-28.md) | Superseded readiness report. |
| [`PRODUCTION_SCORE_2026-07-24.md`](reports/PRODUCTION_SCORE_2026-07-24.md) | Superseded scoring. |
| [`PERFORMANCE_RELIABILITY_AUDIT.md`](reports/PERFORMANCE_RELIABILITY_AUDIT.md) | Performance and reliability analysis. |
| [`BENCHMARK_100_100_EVIDENCE.md`](reports/BENCHMARK_100_100_EVIDENCE.md) | Benchmark evidence (historical). |
| [`IMPROVEMENTS_100_PERFECT.md`](reports/IMPROVEMENTS_100_PERFECT.md) | Improvement log (historical). |
| [`MODEL_AVAILABILITY.md`](reports/MODEL_AVAILABILITY.md) | Model availability notes. |
| [`PLAN_MODEL_INTELLIGENCE_2026-07-24.md`](reports/PLAN_MODEL_INTELLIGENCE_2026-07-24.md) | Model-registry design plan. |
| [`ENV_SYNC_2026-07-24.md`](reports/ENV_SYNC_2026-07-24.md) | Environment variable sync notes. |

## `docs/artifacts/` — one-off scripts and raw output

Not production code, not part of the test suite. Kept for provenance.

| File | Purpose |
|---|---|
| `fleet_validation_summary.json` | Pre-fix baseline validation summary (66ec1c1). |
| `fleet_snapshot_diagnostics.json` | Pre-fix hot-swap diagnostics: 15 FAIL findings across 4 providers. |
| `verify_fleet_audit.py` | Pre-fix diagnostics reproducer. |
| `fleet_validation_summary_post_fix.json` | Post-fix gate summary (unit 1366/0, acceptance 260/260, hot swap 40/40, churn bounded). |
| `fleet_snapshot_diagnostics_post_fix.json` | Post-fix hot-swap diagnostics: 24/24 PASS. |
| `verify_fleet_audit_post_fix.py` | Post-fix diagnostics reproducer. |
| `hotswap_acceptance_fleet.json` | Live hot-swap acceptance evidence (10 scenarios × 4 providers, PID pinned). |
| `acceptance_fleet_post_fix.json` | Full-fleet native acceptance evidence (65 × 4). |
| `acceptance_nvidia_pre_fix.json` | NVIDIA acceptance pre-fix (61/65, 4 streaming failures). |
| `full_matrix_post_fix.json` | SDK full matrix evidence (192/192). |
| `compat_layer_e2e_post_fix.log` | Compatibility layer 2/3 E2E evidence (4 wrappers). |
| `key_churn_profile.json` + `key_churn_profile.py` | 300-generation key-churn resource bound evidence + reproducer. |
| `fleet_dependency_conflicts.json` | Pre-fix Coarena dependency conflict evidence (resolved in this round). |
| `coarena_snapshot_checks.json` + `verify_coarena_snapshot.py` | Coarena integration snapshot checks. |
| `test_nvidia_llms.py` | Ad-hoc script that probed every NVIDIA text model. |
| `retry_nvidia_failed.py` | Ad-hoc retry pass for models that failed the probe. |
| `update_readmes.py` | One-time generator used during standardization. |
| `nvidia_llm_test_report.json` | Raw output of the probe. |
| `nvidia_glm52_test_report.json` | Raw output of a GLM-specific probe. |
| `nvidia_retry_report.json` | Raw output of the retry pass. |

---

## Documentation that intentionally lives elsewhere

| Location | Why |
|---|---|
| `productions/` | Operational runbook and production audit tooling — used during deploys. |
| `<wrapper>/README.md` | Per-wrapper docs ship next to the wrapper. |
| `nvidia-python/MIGRATION.md` | Migration notes specific to that wrapper. |
| `tests/e2e_runtime/` | Live E2E and soak harnesses (executable, not documentation). |
