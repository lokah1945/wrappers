#!/usr/bin/env bash
# ============================================================================
# wrapper-nvidia Node.js BACKUP script
# ============================================================================
set -euo pipefail

PROJECT_DIR="/root/wrapper/nvidia"
OUTPUT_DIR="${1:-/root/wrapper/nvidia/backups}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_NAME="wrapper-nvidia-backup-${TIMESTAMP}"
STAGING_DIR=$(mktemp -d)
BACKUP_DIR="${STAGING_DIR}/${BACKUP_NAME}"

log()  { printf "\033[1;34m[BACKUP]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[BACKUP]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[BACKUP]\033[0m %s\n" "$*" >&2; exit 1; }

cleanup() { rm -rf "${STAGING_DIR}"; }
trap cleanup EXIT

log "=== wrapper-nvidia Node.js backup ==="
mkdir -p "${BACKUP_DIR}"/project

log "Copying project source..."
rsync -a --exclude='node_modules' --exclude='backups' --exclude='metrics.db'   "${PROJECT_DIR}/" "${BACKUP_DIR}/project/"

log "Copying metrics.db..."
if [ -f "${PROJECT_DIR}/metrics.db" ]; then
  cp "${PROJECT_DIR}/metrics.db" "${BACKUP_DIR}/project/metrics.db"
fi

log "Copying .env..."
if [ -f "${PROJECT_DIR}/.env" ]; then
  cp "${PROJECT_DIR}/.env" "${BACKUP_DIR}/project/.env"
fi

log "Creating metadata..."
cat > "${BACKUP_DIR}/metadata.json" << EOF
{
  "backup_name": "${BACKUP_NAME}",
  "backup_timestamp": "$(date -Iseconds)",
  "source_directory": "${PROJECT_DIR}",
  "wrapper_version": "4.4.0-node",
  "node_version": "$(node --version)",
  "os_version": "$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"' || echo unknown)"
}
EOF

mkdir -p "${OUTPUT_DIR}"
ARCHIVE_PATH="${OUTPUT_DIR}/${BACKUP_NAME}.tar.gz"
cd "${STAGING_DIR}" && tar -czf "${ARCHIVE_PATH}" "${BACKUP_NAME}"
SHA256=$(sha256sum "${ARCHIVE_PATH}" | cut -d' ' -f1)
echo "${SHA256}  ${BACKUP_NAME}.tar.gz" > "${ARCHIVE_PATH}.sha256"

log "Backup completed: ${ARCHIVE_PATH}"
