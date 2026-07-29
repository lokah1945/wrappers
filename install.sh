#!/usr/bin/env bash
# install.sh — canonical installer for the wrappers monorepo.
#
# Installs one or all Python wrapper services using the same layout/contract:
#   nvidia-python  -> port 9101
#   nous           -> port 9102
#   opencode       -> port 9103
#   blackbox       -> port 9104
#   openrouter     -> port 9106
#   model-registry -> port 9200
#
# Usage:
#   sudo ./install.sh                         # install all wrappers
#   sudo ./install.sh --wrapper blackbox      # install one wrapper
#   sudo ./install.sh --status                # status for all wrappers
#   sudo ./install.sh --wrapper nous --status # status for one wrapper
#   sudo ./install.sh --no-restart            # copy units/install deps only

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
WRAPPER="all"

while [ $# -gt 0 ]; do
  case "$1" in
    --wrapper) WRAPPER="${2:-}"; shift 2 ;;
    --status|status) MODE="status"; shift ;;
    --no-restart) MODE="no-restart"; shift ;;
    *) echo "[install][ERROR] unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install][ERROR] %s\n' "$*" >&2; exit 1; }

# name|dir|unit|health
WRAPPERS=(
  "nvidia-python|nvidia-python|wrapper-nvidia-python.service|http://127.0.0.1:9101/health"
  "nous|nous|wrapper-nous.service|http://127.0.0.1:9102/health"
  "opencode|opencode|wrapper-opencode.service|http://127.0.0.1:9103/health"
  "blackbox|blackbox|wrapper-blackbox.service|http://127.0.0.1:9104/health"
  "openrouter|openrouter|wrapper-openrouter.service|http://127.0.0.1:9106/health"
  "model-registry|model-registry|wrapper-model-registry.service|http://127.0.0.1:9200/health"
)

selected_wrappers() {
  for item in "${WRAPPERS[@]}"; do
    IFS='|' read -r name dir unit health <<<"$item"
    if [ "$WRAPPER" = "all" ] || [ "$WRAPPER" = "$name" ] || [ "$WRAPPER" = "$dir" ]; then
      printf '%s\n' "$item"
    fi
  done
}

if [ -z "$(selected_wrappers)" ]; then
  fail "unknown wrapper: ${WRAPPER}"
fi

if [ "$MODE" = "status" ]; then
  while IFS='|' read -r name dir unit health; do
    log "status ${unit}"
    systemctl --user is-active "$unit" 2>&1 || true
    systemctl --user is-enabled "$unit" 2>&1 || true
    systemctl --user show "$unit" -p MainPID,NRestarts,ActiveState,SubState --no-pager 2>&1 || true
  done < <(selected_wrappers)
  exit 0
fi

if [ "$(id -u)" -eq 0 ] && [ "${HOME:-}" = "/root" ]; then
  log "INFO: running as root — units install to /root/.config/systemd/user (user-level systemd for root)"
fi

# Deployment model: USER-LEVEL systemd (per wrapper runtime reality).
# Services run under the invoking user's manager, not the system manager.
# Use $HOME when available (works for non-root deploys too); fall back to /root.
USER_UNIT_DIR="${HOME:-/root}/.config/systemd/user"
mkdir -p "$USER_UNIT_DIR"

while IFS='|' read -r name dir unit health; do
  src_dir="${PROJECT_DIR}/${dir}"
  unit_src="${src_dir}/systemd/${unit}"
  unit_dst="${USER_UNIT_DIR}/${unit}"
  [ -d "$src_dir" ] || fail "missing wrapper dir: $src_dir"
  [ -f "$unit_src" ] || fail "missing systemd unit: $unit_src"

  # Auto-bootstrap .env from .env.example if missing (operator still needs
  # to fill in credentials, but at least the service can boot to a clear
  # 'missing required env' error instead of failing to find .env).
  if [ ! -f "${src_dir}/.env" ] && [ -f "${src_dir}/.env.example" ]; then
    log "WARN: ${dir}/.env missing — copying from .env.example (edit credentials before runtime)"
    cp "${src_dir}/.env.example" "${src_dir}/.env"
    chmod 600 "${src_dir}/.env"
  elif [ ! -f "${src_dir}/.env" ]; then
    log "WARN: ${dir}/.env missing and no .env.example available — service may fail to boot"
  fi

  if [ -f "${src_dir}/requirements.txt" ]; then
    log "installing Python deps for ${name}"
    python3 -m pip install -r "${src_dir}/requirements.txt"
  fi

  # Unit files are kept portable in git. Render the repository path used by
  # this installation instead of silently hardcoding /root/wrapper.
  rendered_unit="$(mktemp)"
  # F13 fix: refuse paths containing the sed delimiter instead of silently
  # producing a broken unit file.
  case "$PROJECT_DIR" in
    *'#'*) fail "PROJECT_DIR must not contain '#': $PROJECT_DIR" ;;
  esac
  escaped_project="${PROJECT_DIR//&/\\&}"
  sed "s#/root/wrapper#${escaped_project}#g" "$unit_src" > "$rendered_unit"
  install -m 0644 "$rendered_unit" "$unit_dst"
  rm -f "$rendered_unit"
  log "rendered ${unit_src} -> ${unit_dst} (${PROJECT_DIR})"
  systemctl --user daemon-reload
  systemctl --user enable "$unit" 2>&1 || log "WARN: enable failed for ${unit}"
done < <(selected_wrappers)

log "systemd user daemon-reload OK"

if [ "$MODE" = "no-restart" ]; then
  log "skipping restart (--no-restart)"
  exit 0
fi

# F13 fix: do not abort mid-deploy on the first unhealthy wrapper — that
# left the remaining services running old code. Restart all, then report.
FAILED_UNITS=""
while IFS='|' read -r name dir unit health; do
  log "restarting ${unit}"
  systemctl --user reset-failed "$unit" 2>&1 || true
  systemctl --user restart "$unit"
  sleep 2
  if curl -sS -m 5 "$health" > "/tmp/${unit}.health.json" 2>/dev/null; then
    log "✅ ${name} healthy"
    cat "/tmp/${unit}.health.json"
    printf '\n'
  else
    log "❌ ${name} health failed"
    tail -30 "/tmp/${unit}.health.json" || true
    journalctl --user -u "$unit" --since "30 seconds ago" --no-pager | tail -20 || true
    FAILED_UNITS="${FAILED_UNITS} ${unit}"
  fi
done < <(selected_wrappers)

if [ -n "$FAILED_UNITS" ]; then
  fail "unhealthy after restart:${FAILED_UNITS}"
fi

log "install complete"
