#!/usr/bin/env bash
# The default full suite uses fake upstreams and never calls live providers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/run_production_gates.sh" "$@"
