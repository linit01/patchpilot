#!/bin/bash
#
# If `git status` shows your branch diverged from origin/main, rebase first so the
# release commit sits on top of remote, then run this script:
#   git fetch origin && git rebase origin/main
# (Resolve conflicts if any, then continue.) If you already recreated tag locally,
# the script can delete/recreate the same version tag when prompted.
#
# One-off history rewrite (e.g. after git filter-repo / secret scrub): this script is
# for normal versioned releases only. To publish rewritten main + tags, run manually:
#   git push --force-with-lease origin main && git push --force origin --tags
# Coordinate with anyone else using the repo; all clones need to reset or re-clone.
#
# Git push gate:
#   - Interactive terminal: final yes/no prompt before commit / tag / push (unchanged).
#   - Non-interactive (e.g. CI, agent): commit/tag/push runs ONLY if
#       PATCHPILOT_RELEASE_APPROVED=1
#     otherwise the script updates VERSION / compose / k8s, prints the git commands,
#     then exits 2 without pushing (operator must review and approve, then re-run).
#   Do not pipe "yes" into this script to bypass review.

# Navigate to the project directory
cd /Users/sanborn/github/patchpilot || exit

DOCKERHUB_REPO="linit01/patchpilot"

# Check if VERSION input is provided
if [ -z "$1" ]; then
    read -p "Enter the new version number (e.g. v0.10.0-alpha): " VERSION
else
    VERSION=$1
fi

# Strip leading 'v' for file contents (VERSION file stores without v prefix)
VERSION_CLEAN="${VERSION#v}"

# Check if COMMIT input is provided
if [ -z "$2" ]; then
    # Non-interactive (approved via env var): commit message is required as $2
    if [ "${PATCHPILOT_RELEASE_APPROVED}" = "1" ]; then
        echo "❌  Commit message required when using PATCHPILOT_RELEASE_APPROVED=1"
        echo "    Usage: PATCHPILOT_RELEASE_APPROVED=1 ./scripts/push_new_build.sh <version> \"<message>\""
        echo "    Example: PATCHPILOT_RELEASE_APPROVED=1 ./scripts/push_new_build.sh v0.16.8-beta \"add macOS system update label exclusions\""
        exit 1
    fi
    read -p "Enter the commit message: " COMMIT
else
    COMMIT=$2
fi

# Guard: commit message must not be empty
if [ -z "$COMMIT" ]; then
    echo "❌  Commit message cannot be empty. Describe what changed in this release."
    exit 1
fi

# ── Update VERSION file ──────────────────────────────────────────────────────
echo "$VERSION_CLEAN" > VERSION
echo "✓ VERSION file → $VERSION_CLEAN"

# ── Update image tags in docker-compose.yml ──────────────────────────────────
if [ -f docker-compose.yml ]; then
    sed -i '' "s|${DOCKERHUB_REPO}:backend-[^ ]*|${DOCKERHUB_REPO}:backend-${VERSION_CLEAN}|g" docker-compose.yml
    sed -i '' "s|${DOCKERHUB_REPO}:frontend-[^ ]*|${DOCKERHUB_REPO}:frontend-${VERSION_CLEAN}|g" docker-compose.yml
    echo "✓ docker-compose.yml → backend-${VERSION_CLEAN}, frontend-${VERSION_CLEAN}"
else
    echo "⚠️  docker-compose.yml not found — skipping"
fi

# ── Update image tags in k8s/deployment.yaml ─────────────────────────────────
if [ -f k8s/deployment.yaml ]; then
    sed -i '' "s|${DOCKERHUB_REPO}:backend-[^ ]*|${DOCKERHUB_REPO}:backend-${VERSION_CLEAN}|g" k8s/deployment.yaml
    sed -i '' "s|${DOCKERHUB_REPO}:frontend-[^ ]*|${DOCKERHUB_REPO}:frontend-${VERSION_CLEAN}|g" k8s/deployment.yaml
    echo "✓ k8s/deployment.yaml → backend-${VERSION_CLEAN}, frontend-${VERSION_CLEAN}"
    # Verify the tags were actually updated (backend appears twice: seed-ansible + backend container)
    VERIFY_BE=$(grep -c "backend-${VERSION_CLEAN}" k8s/deployment.yaml)
    VERIFY_FE=$(grep -c "frontend-${VERSION_CLEAN}" k8s/deployment.yaml)
    if [ "$VERIFY_BE" -lt 2 ] || [ "$VERIFY_FE" -lt 1 ]; then
        echo "⚠️  WARNING: deployment.yaml may not have been fully updated!"
        echo "   backend-${VERSION_CLEAN} occurrences: ${VERIFY_BE} (expected 2)"
        echo "   frontend-${VERSION_CLEAN} occurrences: ${VERIFY_FE} (expected 1)"
        echo "   Current image lines:"
        grep "image:" k8s/deployment.yaml | sed 's/^/     /'
    fi
else
    echo "⚠️  k8s/deployment.yaml not found — skipping"
fi

# Prepare git commands to be executed
GIT_ADD="git add -A"
GIT_COMMIT="git commit -m \"${VERSION}: ${COMMIT}\""
GIT_TAG="git tag ${VERSION}"
GIT_PUSH="git push && git push origin ${VERSION}"

# Check if the version tag already exists and delete it if it does
if git show-ref --tags | grep "refs/tags/${VERSION}" > /dev/null; then
    OLD_VERSION=$VERSION
    echo ""
    echo "⚠️  Version tag '${OLD_VERSION}' already exists. Deleting it..."
    GIT_DELETE_TAG="git tag -d ${OLD_VERSION}"
    GIT_PUSH_DELETE_TAG="git push origin :refs/tags/${OLD_VERSION}"

    # Display the exact command that will be executed for deletion
    echo "The following commands will be executed to delete the existing version:"
    echo "  ${GIT_DELETE_TAG}"
    echo "  ${GIT_PUSH_DELETE_TAG}"

    # Ask for user confirmation
    read -p "Are you sure you want to proceed with deletion? (yes/no): " CONFIRMATION

    if [ "$CONFIRMATION" = "yes" ]; then
        eval $GIT_DELETE_TAG
        eval $GIT_PUSH_DELETE_TAG
    else
        echo "Operation cancelled. Exiting."
        exit 1
    fi
fi

# Display the exact commands that will be executed
echo ""
echo "The following commands will be executed:"
echo "  ${GIT_ADD}"
echo "  ${GIT_COMMIT}"
echo "  ${GIT_TAG}"
echo "  ${GIT_PUSH}"
echo ""

RUN_GIT=false
if [ "${PATCHPILOT_RELEASE_APPROVED}" = "1" ]; then
    RUN_GIT=true
    echo "✓ PATCHPILOT_RELEASE_APPROVED=1 — proceeding with commit, tag, and push."
elif [ -t 0 ]; then
    read -p "Are you sure you want to proceed? (yes/no): " CONFIRMATION
    if [ "$CONFIRMATION" = "yes" ]; then
        RUN_GIT=true
    fi
else
    echo ""
    echo "⚠️  Non-interactive stdin: commit / tag / push were NOT run."
    echo "    Review the VERSION, docker-compose.yml, and k8s changes above."
    echo "    After explicit operator approval (e.g. approve or yes in Cursor), re-run:"
    printf '    PATCHPILOT_RELEASE_APPROVED=1 %q %q %q\n' \
        "./scripts/push_new_build.sh" "$VERSION" "$COMMIT"
    echo ""
    echo "Operation stopped before git push (exit 2)."
    exit 2
fi

if [ "$RUN_GIT" = true ]; then
    eval $GIT_ADD
    eval $GIT_COMMIT
    eval $GIT_TAG
    eval $GIT_PUSH
    echo ""
    echo "✅ ${VERSION} pushed. CI will build and publish images."
else
    echo "Operation cancelled."
fi
