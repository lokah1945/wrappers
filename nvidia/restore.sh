#!/usr/bin/env bash
# wrapper-nvidia Node.js restore script
set -euo pipefail

SOURCE_DIR="${1:-}"
[ -z "$SOURCE_DIR" ] && { echo "Usage: $0 <backup_dir>"; exit 1; }

PROJECT_DIR="/root/wrapper/nvidia"
log()  { printf "\033[1;34m[RESTORE]\033[0m %s\n" "$*"; }

log "Restoring from ${SOURCE_DIR}..."
rsync -a "${SOURCE_DIR}/project/" "${PROJECT_DIR}/"
log "Restore completed."
