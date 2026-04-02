#!/usr/bin/env bash
# Run once after `git filter-repo` secret scrub: updates GitHub to match rewritten history.
# WARNING: This replaces remote history. All clones and forks must re-fetch or re-clone.
# Rotate any secrets that were ever in old history (Docker PAT, Fernet key) even after scrub.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Add origin first, e.g.: git remote add origin https://github.com/USER/patchpilot.git"
  exit 1
fi

echo "Remote: $(git remote get-url origin)"
echo "Will force-push main and all tags."
echo ""
if [[ "${PATCHPILOT_HISTORY_REWRITE_PUSH:-}" != "1" ]]; then
  echo "Set PATCHPILOT_HISTORY_REWRITE_PUSH=1 to confirm, then re-run:"
  echo "  PATCHPILOT_HISTORY_REWRITE_PUSH=1 $0"
  exit 2
fi

git push --force-with-lease origin main
git push --force origin --tags

echo "Done. Delete or reset any stale remote branches you no longer need."
