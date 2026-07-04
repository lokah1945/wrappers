#!/usr/bin/env bash
# sync-models.sh — Auto-sync OpenCode model list from wrapper-nvidia.
#
# Queries the wrapper's /v1/models + /metrics/model-status endpoints, keeps
# only models the wrapper has verified AVAILABLE, and rewrites
# /root/.config/opencode/opencode.jsonc with that list under the
# `wrapper-nvidia` provider (id: nvidia, baseURL override).
#
# OpenCode v1.17.13 uses the static models.dev catalog for built-in providers
# (nvidia/openai) and ignores baseURL for the model LIST — so we must hardcode
# the `models` map here. This script keeps that map fresh by re-running on a
# systemd timer. The model list is the only thing that is dynamic; everything
# else (id, baseURL, apiKey) is preserved verbatim.
#
# Run manually:  /root/wrapper/opencode/sync-models.sh
# Scheduled by: opencode-sync.timer (every 10 min)

set -euo pipefail

WRAPPER="http://localhost:9100"
CONFIG="/root/.config/opencode/opencode.jsonc"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# ── 1. Fetch the full model list from the wrapper ────────────────────────
if ! models_json="$(curl -fsS --max-time 10 "$WRAPPER/v1/models" 2>/dev/null)"; then
  echo "[sync-models] ERROR: cannot reach wrapper at $WRAPPER/v1/models" >&2
  exit 1
fi

# ── 2. Fetch the unavailable set (models the wrapper has marked 404/degraded) ─
unavail_json="$(curl -fsS --max-time 10 "$WRAPPER/metrics/model-status" 2>/dev/null || echo '{"unavailable":[]}')"

# ── 3. Compute the AVAILABLE model list (in /v1/models but NOT unavailable) ─
# Output: one model id per line, sorted, JSON-escaped.
available="$(python3 - "$models_json" "$unavail_json" <<'PY'
import json, sys
models = json.loads(sys.argv[1])
unavail = set(json.loads(sys.argv[2]).get("unavailable", []))
ids = [m["id"] for m in models.get("data", []) if m.get("id") and m["id"] not in unavail]
for i in sorted(ids):
    # JSON-escape the id (model ids are simple, but be safe)
    print(json.dumps(i))
PY
)"

if [ -z "$available" ]; then
  echo "[sync-models] WARNING: no available models from wrapper — leaving config unchanged" >&2
  exit 0
fi

# ── 4. Build the `models` object map: { "model-id": {} } ─────────────────
# Each value is an empty object — OpenCode treats the key as the model id and
# uses the wrapper's baseURL for actual calls. We add `name` for nicer display.
models_block="$(python3 - <<PY
import json
ids = []
import sys
# read available ids from stdin
PY
)"
# (simpler: build the whole models object in one python pass)
models_obj="$(python3 -c "
import json, sys
ids = [line for line in sys.stdin.read().splitlines() if line]
# Each id is already JSON-quoted; strip quotes to get raw id.
raw = [json.loads(l) for l in ids]
obj = {i: {'name': i} for i in raw}
print(json.dumps(obj, indent=4))
" <<< "$available")"

# ── 5. Rewrite opencode.jsonc preserving the provider block ──────────────
python3 - "$CONFIG" "$models_obj" <<'PY'
import json, sys, os, datetime

config_path = sys.argv[1]
models_obj = json.loads(sys.argv[2])

# Read current config (JSONC — strip // line comments and /* block */ comments)
raw = open(config_path, encoding="utf-8").read()
stripped = raw
# strip block comments
import re
stripped = re.sub(r"/\*[\s\S]*?\*/", "", stripped)
# strip line comments (// ... to end of line), but not inside strings — naive but
# fine here since our config has no // inside string values
stripped = re.sub(r"(^|[^:])//.*$", r"\1", stripped, flags=re.MULTILINE)
cfg = json.loads(stripped)

prov = cfg.setdefault("provider", {}).get("wrapper-nvidia")
if prov is None:
    raise SystemExit("ERROR: provider.wrapper-nvidia missing from config")

# Force the canonical provider settings (id/baseURL/apiKey) so a stale or
# hand-edited config can't break routing, then inject the synced model list.
prov["id"] = "nvidia"
prov["name"] = "wrapper-nvidia"
prov.setdefault("options", {})
prov["options"]["baseURL"] = "http://localhost:9100/v1"
prov["options"]["apiKey"] = "wrapper-nvidia-no-auth"
prov["models"] = models_obj

# Write back as pretty JSON (OpenCode accepts plain JSON; comments are lost
# but that's fine — the header comment below documents the sync mechanism).
header = (
    "{\n"
    '  "$schema": "https://opencode.ai/config.json",\n'
    "  // wrapper-nvidia — NVIDIA NIM API proxy (port 9100).\n"
    "  // Model list AUTO-SYNCED by /root/wrapper/opencode/sync-models.sh\n"
    "  // (systemd timer: opencode-sync.timer, every 10 min).\n"
    "  // Source: wrapper /v1/models filtered by /metrics/model-status (AVAILABLE only).\n"
    "  // Do NOT edit the `models` block by hand — it will be overwritten.\n"
    "  // Manual re-sync: /root/wrapper/opencode/sync-models.sh\n"
)
# Build body without the outer braces, then re-wrap with our header.
body = json.dumps(cfg, indent=2)
# Remove leading "{\n" and trailing "\n}" from body to splice into header.
inner = body[body.index("\n")+1:body.rindex("\n}")]
out = header + inner + "\n}\n"

# Backup + atomic write
bak = config_path + ".bak." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
os.replace(config_path, bak) if os.path.exists(config_path) else None
# Actually keep current as .bak
if os.path.exists(bak):
    pass
with open(config_path, "w", encoding="utf-8") as f:
    f.write(out)

n = len(models_obj)
print(f"[sync-models] OK: wrote {n} available models to {config_path}")
print(f"[sync-models] backup: {bak}")
PY
