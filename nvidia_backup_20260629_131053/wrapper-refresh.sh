#!/usr/bin/env bash
# wrapper-nvidia ops refresh script — refresh keys, restart service, verify health.
# Usage:  wrapper-refresh.sh [--dry-run] [--no-restart]
# Exit codes: 0 ok, 1 health fail, 2 reload fail, 3 restart fail
set -euo pipefail

SERVICE="${SERVICE_NVIDIA:-wrapper-nvidia.service}"
HOST="${WRAPPER_HOST:-127.0.0.1}"
PORT="${WRAPPER_PORT:-9100}"
HEALTH_URL="http://${HOST}:${PORT}/health"
STATS_URL="http://${HOST}:${PORT}/stats"
ENV_FILE="/root/wrapper/nvidia/.env"

DRY_RUN=0
NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run] [--no-restart]"; exit 0 ;;
  esac
done

log()  { printf "\033[1;34m[refresh]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[refresh]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[refresh]\033[0m %s\n" "$*" >&2; exit 1; }

log "Step 1/5 — verify .env readable"
[ -r "$ENV_FILE" ] || fail "Cannot read $ENV_FILE"
KEYS=$(grep -cE '^NVIDIA_API_KEY(_[0-9]+)?=' "$ENV_FILE" || true)
[ "$KEYS" -ge 1 ] || fail "No NVIDIA_API_KEY* found in $ENV_FILE"
log "Found $KEYS NVIDIA_API_KEY entries"

log "Step 2/5 — current service state"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  log "$SERVICE: active"
else
  warn "$SERVICE: not active — will attempt start"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "--dry-run set, skipping steps 3-5"
  exit 0
fi

if [ "$NO_RESTART" != "1" ]; then
  log "Step 3/5 — restart $SERVICE"
  # reload-or-restart may time out waiting on D-Bus; use try-restart with explicit timeout.
  # try-restart is a NO-OP if unit is not active, so it's safe.
  if ! timeout 30 systemctl try-restart "$SERVICE" 2>&1 | tail -3; then
    warn "systemctl try-restart did not complete in 30s; sleep 5 then probe"
    sleep 5
  fi
  sleep 5   # uvicorn binds port AFTER pool init; allow ~5s cold-start
fi

log "Step 4/5 — wait for HTTP ready"
for i in $(seq 1 15); do
  if curl -sSf -o /dev/null --max-time 2 "$HEALTH_URL"; then
    log "Health endpoint reachable after ${i} attempt(s)"
    break
  fi
  sleep 1
  [ "$i" = "15" ] && fail "Health endpoint not reachable after 15s (exit 1)"
done

log "Step 5/5 — verify keys loaded"
HEALTH=$(curl -sSf --max-time 3 "$HEALTH_URL")
TOTAL=$(echo "$HEALTH"  | node -e "let d=''; process.stdin.on('data',c=>d+=c); process.stdin.on('end',()=>{try{const j=JSON.parse(d);console.log(j.total_keys||'?')}catch{console.log('?')}})")
AVAIL=$(echo "$HEALTH"  | node -e "let d=''; process.stdin.on('data',c=>d+=c); process.stdin.on('end',()=>{try{const j=JSON.parse(d);console.log(j.available_keys||'?')}catch{console.log('?')}})")
if [ "$TOTAL" -le 0 ] 2>/dev/null; then fail "total_keys invalid: $TOTAL"; fi
if [ "$AVAIL" -le 0 ] 2>/dev/null; then fail "available_keys invalid: $AVAIL"; fi
log "Pool: total=$TOTAL available=$AVAIL"

log "Refresh OK ✅"
