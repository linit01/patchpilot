#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — Claude Context Generator
#
# Generates a base64-encoded tarball of the PatchPilot source for attaching
# directly in a Claude chat session. Binary tarballs are corrupted by Claude's
# project knowledge indexer — base64 encoding keeps it text-safe.
#
# Usage:
#   ./scripts/claude-context.sh
#
# Output:
#   ~/patchpilot.tgz.b64  — attach this file directly in Claude chat
#
# NOTE: Do NOT upload to Claude project knowledge — attach in chat only.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${REPO_ROOT}/.." && pwd)"
REPO_NAME="$(basename "${REPO_ROOT}")"
OUTPUT_TGZ=~/patchpilot.tgz
OUTPUT_B64=~/patchpilot.tgz.b64

echo "▸ Building Claude context from ${REPO_ROOT}"

# Archive old
mv "${OUTPUT_TGZ}" ~/patchpilot-old-$(date -Ihours).tgz 2>/dev/null || true
mv "${OUTPUT_B64}" ~/patchpilot-old-$(date -Ihours).tgz.b64 2>/dev/null || true

# Create tarball
cd "${PARENT_DIR}"
COPYFILE_DISABLE=1 tar \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='.env' \
  --exclude='*.key' \
  --exclude='*.pem' \
  --exclude='k8s/.generated' \
  --exclude="${REPO_NAME}/.venv" \
  --exclude="${REPO_NAME}/venv" \
  --exclude="${REPO_NAME}/node_modules" \
  -czf "${OUTPUT_TGZ}" \
  "${REPO_NAME}"

# Base64 encode for text-safe upload
base64 -i "${OUTPUT_TGZ}" -o "${OUTPUT_B64}"

TGZ_SIZE="$(du -sh "${OUTPUT_TGZ}" | cut -f1)"
B64_SIZE="$(du -sh "${OUTPUT_B64}" | cut -f1)"

echo ""
echo "✓ Created ${OUTPUT_TGZ} (${TGZ_SIZE})"
echo "✓ Created ${OUTPUT_B64} (${B64_SIZE})"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Attach ~/patchpilot.tgz.b64 directly in Claude chat"
echo "  Do NOT upload to Claude project knowledge files"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
