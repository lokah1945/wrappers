#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────
# switch-to-nodejs.sh — 1-command switch: Python wrapper → Node.js wrapper
# 
# Usage:
#   ./switch-to-nodejs.sh          # Switch to Node.js on port 9100
#   ./switch-to-nodejs.sh --test   # Start Node.js on BETA port 9101 (Python still runs)
#   ./switch-to-nodejs.sh --rollback  # Switch back to Python
# ───────────────────────────────────────────────────────────────────────
set -euo pipefail

PYTHON_DIR="/root/wrapper/nvidia_python_backup_20260627"
NODE_DIR="/root/wrapper/nvidia"
PRODUCTION_PORT=9100
BETA_PORT=9101
SERVICE_NAME="wrapper-nvidia"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${CYAN}[SWITCH]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

find_python_pid() {
  pgrep -f "python3.*nvidia_python_backup_20260627/main.py" 2>/dev/null || true
}

find_node_pid() {
  pgrep -f "node.*${NODE_DIR}/src/index.js" 2>/dev/null || true
}

stop_python() {
  local pid
  pid=$(find_python_pid)
  if [[ -n "$pid" ]]; then
    log "Stopping Python wrapper (PID: $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 2
    # Verify stopped
    if [[ -n "$(find_python_pid)" ]]; then
      warn "Python still running, force kill..."
      kill -9 "$(find_python_pid)" 2>/dev/null || true
      sleep 1
    fi
    ok "Python wrapper stopped"
  else
    warn "No Python wrapper process found"
  fi
}

stop_node() {
  local pid
  pid=$(find_node_pid)
  if [[ -n "$pid" ]]; then
    log "Stopping Node.js wrapper (PID: $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 2
    if [[ -n "$(find_node_pid)" ]]; then
      warn "Node.js still running, force kill..."
      kill -9 "$(find_node_pid)" 2>/dev/null || true
      sleep 1
    fi
    ok "Node.js wrapper stopped"
  else
    warn "No Node.js wrapper process found"
  fi
}

start_node_production() {
  log "Starting Node.js wrapper on PRODUCTION port ${PRODUCTION_PORT}..."
  cd "$NODE_DIR"
  LISTEN_PORT=${PRODUCTION_PORT} nohup node src/index.js > /root/wrapper/nvidia-node/wrapper-nvidia.log 2>&1 &
  local pid=$!
  sleep 4

  # Verify
  if curl -sf "http://127.0.0.1:${PRODUCTION_PORT}/health" > /dev/null 2>&1; then
    ok "Node.js wrapper RUNNING on port ${PRODUCTION_PORT} (PID: $pid)"
    ok "Production is now served by Node.js!"
  else
    err "Node.js wrapper FAILED to start. Check ${NODE_DIR}/wrapper-nvidia.log"
  fi
}

start_node_beta() {
  log "Starting Node.js wrapper on BETA port ${BETA_PORT}..."
  cd "$NODE_DIR"
  LISTEN_PORT=${BETA_PORT} nohup node src/index.js > /root/wrapper/nvidia-node/wrapper-nvidia-beta.log 2>&1 &
  local pid=$!
  sleep 4

  if curl -sf "http://127.0.0.1:${BETA_PORT}/health" > /dev/null 2>&1; then
    ok "Node.js wrapper RUNNING on BETA port ${BETA_PORT} (PID: $pid)"
    ok "Python wrapper still running on port ${PRODUCTION_PORT}"
    ok "Test Node.js at: http://127.0.0.1:${BETA_PORT}"
  else
    err "Node.js wrapper FAILED to start on BETA port. Check ${NODE_DIR}/wrapper-nvidia-beta.log"
  fi
}

start_python() {
  log "Starting Python wrapper on port ${PRODUCTION_PORT}..."
  cd "$PYTHON_DIR"
  nohup python3 main.py > /root/wrapper/nvidia/wrapper-nvidia.log 2>&1 &
  local pid=$!
  sleep 4

  if curl -sf "http://127.0.0.1:${PRODUCTION_PORT}/health" > /dev/null 2>&1; then
    ok "Python wrapper RUNNING on port ${PRODUCTION_PORT} (PID: $pid)"
    ok "Production reverted to Python!"
  else
    err "Python wrapper FAILED to start. Check ${PYTHON_DIR}/wrapper-nvidia.log"
  fi
}

show_status() {
  echo ""
  echo "┌─────────────────────────────────────────────────┐"
  echo "│         wrapper-nvidia STATUS                    │"
  echo "├─────────────────────────────────────────────────┤"
  local py_pid=$(find_python_pid)
  local nd_pid=$(find_node_pid)
  printf "│ %-15s │ %-10s │ %-8s │\n" "Runtime" "PID" "Port"
  printf "│ %-15s │ %-10s │ %-8s │\n" "───────────────" "──────────" "────────"
  if [[ -n "$py_pid" ]]; then
    printf "│ %-15s │ %-10s │ %-8s │\n" "Python" "$py_pid" "${PRODUCTION_PORT}"
  else
    printf "│ %-15s │ %-10s │ %-8s │\n" "Python" "stopped" "-"
  fi
  if [[ -n "$nd_pid" ]]; then
    local nd_port=$(grep -o 'LISTEN_PORT=[0-9]*' /proc/$nd_pid/environ 2>/dev/null | cut -d= -f2 || echo "?")
    printf "│ %-15s │ %-10s │ %-8s │\n" "Node.js" "$nd_pid" "$nd_port"
  else
    printf "│ %-15s │ %-10s │ %-8s │\n" "Node.js" "stopped" "-"
  fi
  echo "└─────────────────────────────────────────────────┘"
}

# ── Main ──

case "${1:-switch}" in
  --test)
    log "BETA MODE: Starting Node.js on port ${BETA_PORT} (Python untouched)"
    start_node_beta
    show_status
    ;;
  --switch|switch)
    log "SWITCHING: Python → Node.js on production port ${PRODUCTION_PORT}"
    # 1. Stop Node.js beta if running on any port
    stop_node
    # 2. Stop Python
    stop_python
    # 3. Start Node.js on production port
    start_node_production
    show_status
    ;;
  --rollback|rollback)
    log "ROLLBACK: Node.js → Python on production port ${PRODUCTION_PORT}"
    stop_node
    start_python
    show_status
    ;;
  --status|status)
    show_status
    ;;
  --install-service|install-service)
    log "Installing systemd service..."
    cp "${NODE_DIR}/wrapper-nvidia.service" /etc/systemd/system/wrapper-nvidia.service
    systemctl daemon-reload
    systemctl enable wrapper-nvidia.service
    ok "Systemd service installed and enabled"
    log "Note: Service uses port 9100. Start with: systemctl start wrapper-nvidia"
    ;;
  *)
    echo "Usage: $0 {--test|--switch|--rollback|--status|--install-service}"
    echo ""
    echo "  --test            Start Node.js on BETA port ${BETA_PORT} (Python untouched)"
    echo "  --switch          FULL SWITCH: Stop Python, start Node.js on port ${PRODUCTION_PORT}"
    echo "  --rollback        Revert to Python wrapper"
    echo "  --status          Show current status"
    echo "  --install-service Install Node.js as systemd service"
    exit 1
    ;;
esac
