#!/usr/bin/env bash
# install.sh — canonical installer for the wrappers monorepo.
#
# Installs production services enabled in wrappers.json; pending providers are
# frozen and cannot be selected by --wrapper. model-registry is a dependency.
# Production services use the same layout/contract:
#   nvidia-python  -> port 9101
#   nous           -> port 9102
#   openrouter     -> port 9106
#   model-registry -> port 9200
#
# Usage:
#   sudo ./install.sh                         # install production wrappers
#   sudo ./install.sh --wrapper openrouter    # install wrapper + registry
#   sudo ./install.sh --status                # status for all wrappers
#   sudo ./install.sh --wrapper nous --status # status for one wrapper
#   sudo ./install.sh --no-restart            # copy units/install deps only
#   ./install.sh --skip-deps                  # use already-verified dependencies

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
WRAPPER="all"
SKIP_DEPS=false

while [ $# -gt 0 ]; do
  case "$1" in
    --wrapper) WRAPPER="${2:-}"; shift 2 ;;
    --status|status) MODE="status"; shift ;;
    --no-restart) MODE="no-restart"; shift ;;
    --skip-deps) SKIP_DEPS=true; shift ;;
    *) echo "[install][ERROR] unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install][ERROR] %s\n' "$*" >&2; exit 1; }

# Validate metadata before inspecting any provider path or changing systemd.
scope_args=(--repo-dir "$PROJECT_DIR" --format install --include-dashboard)
if [ "$WRAPPER" != "all" ]; then
  scope_args+=(--wrapper "$WRAPPER")
fi
SELECTED_ITEMS="$("${PYTHON_BIN:-python3}" "${PROJECT_DIR}/common/wrapper_scope.py" "${scope_args[@]}")" || exit $?

selected_wrappers() {
  printf '%s\n' "$SELECTED_ITEMS"
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

# Resolve the selected services together before modifying environments or
# units. Sequential installs could silently downgrade another active service's
# verified dependencies. Pending requirements are never visited.
if [ "$SKIP_DEPS" = false ]; then
  dependency_args=()
  while IFS='|' read -r name dir unit health; do
    requirement_file="${PROJECT_DIR}/${dir}/requirements.txt"
    if [ -f "$requirement_file" ]; then
      dependency_args+=(-r "$requirement_file")
    fi
  done < <(selected_wrappers)
  if [ "${#dependency_args[@]}" -gt 0 ]; then
    constraint_file="${PROJECT_DIR}/tests/constraints-verified.txt"
    [ -f "$constraint_file" ] || fail "missing release dependency constraints: $constraint_file"
    log "installing selected Python dependencies in one release-constrained transaction"
    python3 -m pip install -c "$constraint_file" "${dependency_args[@]}"
    python3 -m pip check
  fi
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

  if [ -f "${src_dir}/.env" ]; then
    chmod 600 "${src_dir}/.env"
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
  if ! systemctl --user restart "$unit"; then
    log "❌ ${name} restart failed"
    FAILED_UNITS="${FAILED_UNITS} ${unit}"
    continue
  fi
  probe_args=(--base-url "${health%/health}" --env-file "${PROJECT_DIR}/${dir}/.env")
  if [ "$name" = "model-registry" ]; then
    probe_args+=(--registry)
  fi
  if python3 "${PROJECT_DIR}/productions/check_service_ready.py" "${probe_args[@]}"; then
    log "✅ ${name} ready"
  else
    log "❌ ${name} health/readiness failed"
    FAILED_UNITS="${FAILED_UNITS} ${unit}"
  fi
done < <(selected_wrappers)

if [ -n "$FAILED_UNITS" ]; then
  fail "unhealthy after restart:${FAILED_UNITS}"
fi

log "install complete"
