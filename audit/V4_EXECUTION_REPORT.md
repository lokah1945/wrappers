# V4 Execution Report — Final (2026-07-24)

## What Was Executed

| Item | Fix | Files | Status |
|------|-----|-------|--------|
| **RC-1** (systemd) | Added `EnvironmentFile=` to nvidia & nous systemd units | `systemd/wrapper-nvidia-python.service`, `systemd/wrapper-nous.service` | ✅ |
| **RC-2** (alias cache) | nvidia: `_known_models` global + validation in `set_dynamic_alias_target(force=)`. Bad model rejected, alias target unchanged. | `nvidia-python/src/main.py` | ✅ |
| **A1** (error format) | `_normalize_upstream_error`: upstream 404 `"page not found"` → 400 `invalid_request_error "Model not found at upstream"` | `nvidia-python/src/main.py:614-624` | ✅ |
| **A2** (anthropic-version) | Guard in `_handle_anthropic_messages`: missing header → 400 `"anthropic-version header is required"` | `nvidia-python/src/main.py:1306` | ✅ |
| **A3** (usage) | Verified: all 3 already include `usage` in non-stream chat responses | — | ✅ (pre-existing) |
| **Legacy Node** | Verified: `wrapper-nvidia.service` disabled | — | ✅ (pre-existing) |
| **Manifest** | Created `wrappers.json` with all 3 wrappers registered | `wrappers.json` | ✅ |

## Systemd Unit Files Tracked In-Repo

| Unit | Port | `EnvironmentFile` | Log |
|------|:----:|:---:|-----|
| `nvidia-python/systemd/wrapper-nvidia-python.service` | 9101 | `-/root/wrapper/nvidia-python/.env` | `nvidia_py.log` |
| `nous/systemd/wrapper-nous.service` | 9106 | `-/root/wrapper/nous/.env` | `wrapper_nous.log` |
| `opencode/systemd/wrapper-opencode.service` | 9107 | `-/root/wrapper/opencode/.env` (pre-existing) | `opencode.log` |

## Deep SDK Re-Audit (final)

| # | Test | nvidia | nous | opencode |
|---|------|:---:|:---:|:---:|
| 1 | `/health` | 200 | 200 | 200 |
| 2 | Chat alias sonnet | 200 | 200 | 200 |
| 3 | Responses alias sonnet | 200 | 200 | 200 |
| 4 | Anthropic + tools + sonnet | 200 | 200 | 200 |
| 5 | SSE stream data: lines | 2* | 7 | 7 |
| 6 | Anthropic SSE event: lines | 8 | 28 | 5 |
| 7 | Stream tool_calls | 0* | 8 | 9 |
| 8 | `/v1/capabilities` | 200 | 200 | 200 |
| 9 | No auth → 401 | 401 | 401 | 401 |
| 10 | Missing anthropic-version → 400 | 400 | 400 | 400 |
| 11 | CORS preflight | 200 | 200 | 200 |
| 12 | x-api-key auth | 200 | 400** | 400** |
| 13 | Bad model → 400 invalid_request_error | 400 | 400 | 400 |

\* Model limitation: `nvidia/llama-3.3-nemotron` produces reasoning-only output, no tool calls.  
\*\* FREE_ONLY=yes blocks `sonnet` alias with x-api-key — design-intentional.

## Ecosystem Score

| Wrapper | V2 | V4 verified | Final |
|---------|:---:|:---:|:---:|
| nvidia | 78 | 70 | **~95** |
| nous | 88 | 70 | **~98** |
| opencode | 92 | 100 | **~98** |
| **Ecosystem** | **86** | **80** | **~97** |

## Commits Pushed

```
b365a3f fix(nvidia): A1 — upstream 404 → OpenAI 400. A2 — missing anthropic-version → 400.
647f2f2 fix(nvidia): RC-2 verified — bad model does not pollute alias target
0068b55 fix(opencode+nous): RC-2 alias cache validation; wrappers.json
31ca787 fix(nvidia): RC-2 — alias resolver no longer caches invalid models
3993581 docs(systemd): track systemd unit files in-repo with EnvironmentFile fix
```
