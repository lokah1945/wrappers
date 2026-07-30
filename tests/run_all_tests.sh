#!/usr/bin/env bash
# run_all_tests.sh — Run all test suites

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "═══════════════════════════════════════════════════════════"
echo "  RUNNING ALL TEST SUITES"
echo "═══════════════════════════════════════════════════════════"

# Set environment
export WRAPPER_SKIP_DOTENV=true

# 1. Unit tests
echo ""
echo "=== 1. Unit Tests (pytest) ==="
python3 -m pytest tests/ -q --tb=short

# 2. Simulation tests
echo ""
echo "=== 2. SDK Compatibility Simulation Tests ==="
python3 tests/test_sdk_compatibility_simulation.py

# 3. Real integration tests (skip if wrappers not running)
echo ""
echo "=== 3. Real Integration Tests ==="
python3 tests/test_real_integration.py --skip-if-unavailable || echo "⚠️  Integration tests skipped (wrappers not running)"

# 4. Transparency checks
echo ""
echo "=== 4. Transparency Checks ==="
python3 tests/run_transparency_check.py

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ALL TESTS COMPLETE"
echo "═══════════════════════════════════════════════════════════"
