#!/usr/bin/env bash
# install.sh — canonical installer for wrapper-nvidia
# Runs as root in the wrapper collection. Idempotent.
#
# What this does:
#  1. Validate .env exists (warn if missing)
#  2. Sync canonical /etc/systemd/system/wrapper-nvidia.service
#  3. Remove duplicate /etc/systemd/system/nvidia-wrapper.service if present
#  4. Install node_modules npm ci if missing
#  5. systemd daemon-reload + enable + restart
#  6. Smoke-test /health
#
# Usage:
#   ./install.sh              # full install + restart + smoke test
#   ./install.sh --status     # only show service status (no changes)
#   ./install.sh --no-restart # install (copy unit, daemon-reload) but DO NOT restart
#
set -eu

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="wrapper-nvidia.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
INTERNAL_UNIT="${PROJECT_DIR}/${SERVICE_NAME}"
DUPLICATE_UNIT="/etc/systemd/system/nvidia-wrapper.service"

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install][ERROR] %s\n' "$*" >&2; exit 1; }

MODE="${1:-install}"

# ---- mode: status (no changes) ----
if [ "${MODE}" = "--status" ] || [ "${MODE}" = "status" ]; then
  systemctl is-active "${SERVICE_NAME}" 2>&1 || true
  systemctl is-enabled "${SERVICE_NAME}" 2>&1 || true
  systemctl show "${SERVICE_NAME}" -p MainPID,NRestarts,ActiveState,SubState --no-pager 2>&1 || true
  exit 0
fi

# ---- main install path ----
if [ "$(id -u)" -ne 0 ]; then
  fail "must be root (use sudo)"
fi

[ -f "${PROJECT_DIR}/.env" ] || log "WARN: .env not found at ${PROJECT_DIR}/.env — populate before starting"

# Sync canonical service file
if [ -f "${INTERNAL_UNIT}" ]; then
  cp -f "${INTERNAL_UNIT}" "${SERVICE_PATH}"
  log "synced ${INTERNAL_UNIT} -> ${SERVICE_PATH}"
else
  log "WARN: no internal unit at ${INTERNAL_UNIT}; using whatever is in ${SERVICE_PATH}"
fi

# Remove duplicate nvidia-wrapper.service if it exists
if [ -f "${DUPLICATE_UNIT}" ]; then
  log "removing duplicate unit ${DUPLICATE_UNIT}"
  systemctl stop  nvidia-wrapper.service 2>/dev/null || true
  systemctl disable nvidia-wrapper.service 2>/dev/null || true
  rm -f "${DUPLICATE_UNIT}"
fi

# Install deps if needed
if [ ! -d "${PROJECT_DIR}/node_modules" ]; then
  log "installing node deps (npm ci --omit=dev)..."
  (cd "${PROJECT_DIR}" && npm ci --omit=dev)
else
  log "node_modules present — skipping install"
fi

systemctl daemon-reload
log "systemd daemon-reload OK"

# Enable at boot
systemctl enable "${SERVICE_NAME}" 2>&1 || log "WARN: enable failed"
log "${SERVICE_NAME} will auto-start on boot"

if [ "${MODE}" = "--no-restart" ]; then
  log "skipping restart (--no-restart)"
  exit 0
fi

# Stop orphan (PPID != 1) processes on port 9100 if present
PORT_PID=$(ss -tlnp 2>/dev/null | awk -v port=':9100' '$4 ~ port { gsub(/.*pid=/, "", $7); gsub(/,.*/, "", $7); print $7 }' | head -1)
if [ -n "${PORT_PID:-}" ] && [ "${PORT_PID}" != "" ]; then
  PARENT=$(ps -o ppid= -p "${PORT_PID}" 2>/dev/null | tr -d ' ')
  if [ "${PARENT}" != "1" ] && [ -n "${PARENT}" ]; then
    log "orphan PID ${PORT_PID} (PPID=${PARENT}) holds port 9100 — killing"
    kill -TERM "${PORT_PID}" 2>/dev/null || true
    sleep 2
    kill -KILL "${PORT_PID}" 2>/dev/null || true
  fi
fi

# Reset and start
systemctl reset-failed "${SERVICE_NAME}" 2>&1 || true
systemctl restart "${SERVICE_NAME}"
sleep 2

# Smoke test
if curl -sS -m 5 http://127.0.0.1:9100/health > /tmp/wrapper-health.json 2>&1; then
  log "✅ service healthy:"
  cat /tmp/wrapper-health.json
  printf '\n'
else
  log "❌ service health failed:"
  tail -30 /tmp/wrapper-health.json || true
  journalctl -u "${SERVICE_NAME}" --since "30 seconds ago" --no-pager | tail -20 || true
  exit 1
fi

log "install complete"
