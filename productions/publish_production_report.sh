#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPORT_PATH="${1:-}"
BRANCH="${PRODUCTION_REPORT_BRANCH:-main}"

fail() { printf '[publish][FAIL] %s\n' "$*" >&2; exit 1; }

[ -n "$REPORT_PATH" ] || fail "usage: publish_production_report.sh productions/reports/production-audit-*.md"
case "$REPORT_PATH" in
  /*) report="$REPORT_PATH" ;;
  *) report="${REPO_DIR}/${REPORT_PATH}" ;;
esac
[ -f "$report" ] || fail "report not found: $report"
case "$report" in
  "${REPO_DIR}/productions/reports/"*.md) ;;
  *) fail "report must be inside productions/reports" ;;
esac

cd "$REPO_DIR"
remote_url="$(git remote get-url origin 2>/dev/null || true)"
[ -n "$remote_url" ] || fail "git remote origin is not configured"

# Never publish a report from a dirty code/config checkout. This prevents a
# VPS-local hotfix from being accidentally bundled into a report commit.
dirty="$(git status --porcelain --untracked-files=all | grep -v '^?? productions/reports/' || true)"
[ -z "$dirty" ] || fail "working tree has non-report changes; preserve/review them first"

# Report and staged files must not contain obvious secrets.
if grep -EIn --binary-files=without-match '(MODEL_REGISTRY_ADMIN_TOKEN=\S+|Authorization: Bearer [A-Za-z0-9._-]+|nvapi-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})' "$report" >/dev/null; then
  fail "secret-like value detected in report"
fi

git add "$report"
staged="$(git diff --cached --name-only)"
[ "$staged" = "${report#"$REPO_DIR/"}" ] || fail "staged files are not limited to the selected report"

git diff --cached --check
commit_message="chore: add production audit report $(basename "$report")"
git commit -m "$commit_message"
git push origin "HEAD:${BRANCH}"
remote_head="$(git ls-remote origin "refs/heads/${BRANCH}" | awk '{print $1}')"
local_head="$(git rev-parse HEAD)"
[ "$remote_head" = "$local_head" ] || fail "remote verification failed: local=${local_head} remote=${remote_head}"
printf '[publish][PASS] report committed and pushed: %s\n' "$local_head"
