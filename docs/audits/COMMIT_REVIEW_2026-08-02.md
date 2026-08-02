# Commit Review — `297a510` (docs) + Follow-up `b71d8c3` (docs fixes)

**Date:** 2026-08-02
**Branch:** `arena/019fbee0-wrappers`

---

## 1. Commit Reviewed

```
SHA:       297a51067c30d2da204bf6573a1ed6e959c6b1cd
Date:      Sun Aug 2 01:00:55 2026 +0000
Author:    lokah1945 <243200618+lokah1945@users.noreply.github.com>
Message:   docs: update WRAPPER_CONTRACT v3.1 and all READMEs to current verified state
Parent:    002dc18791db7d9ddb75ee0a2c54be5993c7bd5b
```

**Nature: docs-only.** `git show --stat` confirms 9 files changed, all `.md`
(248 insertions / 90 deletions), **zero** `.py`/`.json`/`.sh` changes. The
dependency chain (shared layer → wrapper layer → SDK behaviour → production
runtime) is therefore **untouched by this commit**.

---

## 2. Change Impact

| File | Change | Risk | Contract Impact |
|---|---|---|---|
| `WRAPPER_CONTRACT.md` | v3.0→v3.1; §11 three→six gates; §12 changelog; references; footer counts | Low (docs) | Claims must match executed gates |
| `README.md` | Status table "Verified by Executable Gates"; run commands; testing section; audit list; version history | Low (docs) | Same |
| `nvidia-python/README.md` | COMPATIBILITY_LAYER section; run command `src.main:app`; status line | Low (docs) | Run command vs §1.1 |
| `nous/README.md` | same pattern | Low (docs) | Same |
| `opencode/README.md` | same pattern (+ port 9107→9103) | Low (docs) | Port vs §9.1 |
| `blackbox/README.md` | same pattern | Low (docs) | Same |
| `openrouter/README.md` | same pattern | Low (docs) | Same |
| `model-registry/README.md` | status note | Low (docs) | n/a |
| `docs/DOCUMENTATION_INDEX.md` | registered COMPATIBILITY_LAYER.md + 4 audit reports | Low (docs) | n/a |

**Modules impacted:** none (no code). **Contract areas impacted by claims:**
§1.1 (run command), §9.1 (ports), §9.2 (COMPATIBILITY_LAYER), §11 (gates),
§12 (changelog), §10 (X-Request-ID).

---

## 3. Claim Cross-Check (docs vs executed reality)

Every numeric/structural claim in the commit was re-verified by running the
actual gates on `297a510` (before the doc-fix commit):

| Claim | Reality (executed) | Verdict |
|---|---|---|
| `python -m pytest tests -q` → **241 tests** | **241 passed** | ✅ |
| Streaming regressions **63** | **63 passed** | ✅ |
| Translation-matrix **63** | **67 passed** | ❌ stale → fixed in `b71d8c3` |
| Runtime E2E **445 checks** | **445/445, 0 failures** | ✅ |
| SDK-compat **5 wrappers × 4 modes** | all parse with official openai SDK | ✅ |
| COMPATIBILITY_LAYER E2E layer 2 + 3 | ✅ all 5 wrappers | ✅ |
| Full matrix audit **240 checks** | **240/240, 0 FAIL, 0 BLOCKED** | ✅ |
| Soak **~20k requests, 0 failures** | **19,947 requests, 0 failures** (3722+3997+4408+4252+3568) | ✅ |
| §11 "all **five** gates" but lists **six** | internal inconsistency | ❌ → fixed in `b71d8c3` |
| §11 "22 pathological… `[26 names]`" | 22 = STREAM_MODES; list mixed non-stream mock modes | ❌ ambiguous → clarified in `b71d8c3` |
| Run command `src.main:app` + ports | matches all systemd units + `wrappers.json` (9101/9102/9103/9104/9106/9200) | ✅ |
| opencode port 9103 | matches `wrappers.json` + systemd | ✅ |

**3 documentation defects found and fixed in follow-up `b71d8c3`** (4
insertions / 4 deletions): "five gates"→"six gates", "63 translation-matrix"→
"67 translation-matrix" (WRAPPER_CONTRACT footer + root README), and the §11
mode-list clarification.

---

## 4. Dependency Chain Analysis

```
Changed Code (297a510) = none (docs only)
      |
      v
Shared Layer (common/*)        ← untouched
      |
      v
Wrapper Layer (5 × src/main)   ← untouched
      |
      v
SDK Client Behaviour           ← verified by SDK-compat gate + matrix audit
      |
      v
Production Runtime             ← verified by 445 E2E + soak
```

Because the commit is docs-only, no wrapper or shared module behaviour changed.
All runtime verification below was executed on the current tree to prove the
docs' claims remain true.

---

## 5. Contract Compliance Audit (current tree)

| Contract area | Evidence | Status |
|---|---|---|
| API contract (§2.1 surfaces) | `test_contract_all_wrappers_expose_required_surfaces` (10 surfaces × 5 wrappers) | ✅ |
| Streaming contract (§3) | 63 streaming regressions + 445 E2E (22 modes incl. `nospace`, `keepalive`, `crlf`, `dupfinish`, `bytesplit`, `midstream_error`) | ✅ |
| Translation contract (§3.6) | 67 translation-matrix tests + 240 matrix audit (real SDKs) | ✅ |
| Authentication (§5) | matrix audit auth-401, fail-closed unit tests | ✅ |
| Resource lifecycle (§6) | soak 0 failures, client-disconnect leak check, `aclose`/`release` patterns present in all wrappers | ✅ |
| Error contract (§3.3) | midstream-error → `response.failed`/`error` event; 500/429 shaped; 429-once retry → 200 | ✅ |
| Tool calling (§3.2) | tools round-trips on all 3 surfaces × both upstream dialects | ✅ |
| SDK compatibility | sdk_codex_compat: 5 wrappers × 4 modes parse with official openai SDK | ✅ |
| §4 input validation (max_tokens/roles) | regression guards + matrix (negative/cap → 400) | ✅ |
| §10 observability (X-Request-ID) | matrix metadata check on all wrappers | ✅ |

---

## 6. Hidden Bug Search (§7)

| Pattern | Result |
|---|---|
| Local helper shadowing shared helper | 0 hits; parity guard passes |
| Duplicate translation logic | shared `common.translations` used; local copies only for provider-specific shapes |
| Unhandled exceptions / silent fallback | 117 `except Exception` reviewed — all in cleanup/best-effort paths; stream translators surface errors as `response.failed`/`error` |
| Missing cleanup | all wrappers release `resp` + pool key in `finally`; openrouter uses `aclose` for generator determinism |
| Incorrect status code | all-keys-exhausted → 429 per contract; malformed input → 4xx (matrix) |
| Protocol leakage | `[DONE]` never leaks into Anthropic passthrough (layer-2 E2E asserts absence); non-object JSON → 400 |

---

## 7. Compatibility Matrix (executed, evidence in `docs/audits/FULL_MATRIX_AUDIT_2026-08-01.json`)

| Agent | Upstream | Mode | Result |
|---|---|---|---|
| OpenAI SDK | OpenAI | Passthrough | ✅ 13 checks/wrapper (param echo-verified) |
| Anthropic SDK | Anthropic | Passthrough | ✅ messages passthrough (stream + non-stream, no `[DONE]`) |
| OpenAI SDK | Anthropic | Translation | ✅ chat + responses via layer 2 (SDK-parsed) |
| Anthropic SDK | OpenAI | Translation | ✅ messages surface (thinking, tools, stop_reason) |
| Codex (openai SDK) | OpenAI | Responses | ✅ SDK-compat gate |
| Claude Code (anthropic SDK) | OpenAI | Messages | ✅ matrix + runtime E2E |

---

## 8. Runtime Verification Executed (this review)

```text
python -m pytest tests -q                                  → 241 passed
python -m pytest tests/test_sse_streaming_regressions.py   → 63 passed
python -m pytest tests/test_translation_matrix.py          → 67 passed
python -m pytest tests/test_compat_layer.py                → 20 passed
python -m pytest tests/test_full_matrix_regressions.py     → 12 passed
python tests/e2e_runtime/run_runtime_e2e.py                → 445/445
python tests/e2e_runtime/sdk_codex_compat.py               → ✅ 5 wrappers × 4 modes
python tests/e2e_runtime/compat_layer_e2e.py               → ✅ layer 2 + 3
python tests/e2e_runtime/full_matrix_audit.py              → 240/240
python tests/e2e_runtime/soak.py                           → 19,947 req, 0 failures
python -m compileall ...                                   → clean
```

---

## 9. Final Status

**✅ VERIFIED WITH EVIDENCE.**

- Commit `297a510` is docs-only; it introduced **no code change**, hence no
  runtime regression is possible from its diff, and all executed gates confirm
  the wrappers remain compliant end-to-end.
- The commit contained **3 factual documentation defects** (six-gate heading
  vs "five", stale translation-matrix count 63 vs actual 67, ambiguous mode
  list), all **fixed and pushed** in follow-up `b71d8c3` and re-verified.
- All 9 contract areas audited pass with executed evidence; the
  wrapper × upstream compatibility matrix (4 quadrants) is green.
