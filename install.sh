#!/usr/bin/env bash
# install.sh — canonical installer for the wrappers monorepo.
#
# Installs one or all Python wrapper services using the same layout/contract:
#   nvidia-python -> port 9101
#   nous          -> port 9106
#   opencode      -> port 9107
#   blackbox      -> port 9108
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
  "nous|nous|wrapper-nous.service|http://127.0.0.1:9106/health"
  "opencode|opencode|wrapper-opencode.service|http://127.0.0.1:9107/health"
  "blackbox|blackbox|wrapper-blackbox.service|http://127.0.0.1:9108/health"
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
    systemctl is-active "$unit" 2>&1 || true
    systemctl is-enabled "$unit" 2>&1 || true
    systemctl show "$unit" -p MainPID,NRestarts,ActiveState,SubState --no-pager 2>&1 || true
  done < <(selected_wrappers)
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  fail "must be root (use sudo)"
fi

while IFS='|' read -r name dir unit health; do
  src_dir="${PROJECT_DIR}/${dir}"
  unit_src="${src_dir}/systemd/${unit}"
  unit_dst="/etc/systemd/system/${unit}"
  [ -d "$src_dir" ] || fail "missing wrapper dir: $src_dir"
  [ -f "$unit_src" ] || fail "missing systemd unit: $unit_src"

  [ -f "${src_dir}/.env" ] || log "WARN: ${dir}/.env missing — copy ${dir}/.env.example and fill credentials before runtime"

  if [ -f "${src_dir}/requirements.txt" ]; then
    log "installing Python deps for ${name}"
    python3 -m pip install -r "${src_dir}/requirements.txt"
  fi

  cp -f "$unit_src" "$unit_dst"
  log "synced ${unit_src} -> ${unit_dst}"
  systemctl enable "$unit" 2>&1 || log "WARN: enable failed for ${unit}"
done < <(selected_wrappers)

systemctl daemon-reload
log "systemd daemon-reload OK"

if [ "$MODE" = "no-restart" ]; then
  log "skipping restart (--no-restart)"
  exit 0
fi

while IFS='|' read -r name dir unit health; do
  log "restarting ${unit}"
  systemctl reset-failed "$unit" 2>&1 || true
  systemctl restart "$unit"
  sleep 2
  if curl -sS -m 5 "$health" > "/tmp/${unit}.health.json" 2>&1; then
    log "✅ ${name} healthy"
    cat "/tmp/${unit}.health.json"
    printf '\n'
  else
    log "❌ ${name} health failed"
    tail -30 "/tmp/${unit}.health.json" || true
    journalctl -u "$unit" --since "30 seconds ago" --no-pager | tail -20 || true
    exit 1
  fi
done < <(selected_wrappers)

log "install complete"
