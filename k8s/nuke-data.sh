#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — nuke-data.sh
# Wipes all node-local hostPath data directories so a fresh deploy starts
# with a clean database and volumes.
#
# Run this ON your k3s node as root before re-deploying.
# Usage:  sudo bash nuke-data.sh
#
# WARNING: DESTRUCTIVE — all PatchPilot PostgreSQL data, Ansible inventory,
# and backups stored on this node will be permanently deleted.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

DATA_DIRS=(
  /app-data/patchpilot-postgres-data
  /app-data/patchpilot-backups
  /app-data/patchpilot-ansible-data
)

PVS=(
  patchpilot-postgres-data
  patchpilot-backups
  patchpilot-ansible-data
)

echo -e "${RED}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  PatchPilot — FULL DATA WIPE                         ║${NC}"
echo -e "${RED}║  This will delete all PatchPilot data on this node.  ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Directories to remove:${NC}"
for d in "${DATA_DIRS[@]}"; do
  if [ -d "$d" ]; then
    echo "  [EXISTS]  $d"
  else
    echo "  [ABSENT]  $d"
  fi
done
echo ""
read -rp "Type 'yes' to confirm wipe: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

# ── Step 1: Delete namespace and PVs ─────────────────────────────────────────
echo ""
echo "── Deleting Kubernetes namespace and PVs ───────────────────────────────"
if kubectl get ns patchpilot &>/dev/null 2>&1; then
  kubectl delete ns patchpilot --ignore-not-found=true
  echo -e "  ${CYAN}Waiting for namespace to fully terminate...${NC}"
  # Wait up to 60s for namespace to disappear
  for i in $(seq 1 30); do
    if ! kubectl get ns patchpilot &>/dev/null 2>&1; then
      echo -e "  ${GREEN}Namespace terminated.${NC}"
      break
    fi
    sleep 2
    echo -n "."
  done
  echo ""
else
  echo "  Namespace patchpilot not found — skipping"
fi

for pv in "${PVS[@]}"; do
  if kubectl get pv "$pv" &>/dev/null 2>&1; then
    kubectl delete pv "$pv" --ignore-not-found=true
    echo -e "  ${GREEN}PV deleted${NC}: $pv"
  else
    echo "  PV not found (skipped): $pv"
  fi
done

# ── Step 2: Remove hostPath data directories ──────────────────────────────────
echo ""
echo "── Removing hostPath data directories ──────────────────────────────────"
for d in "${DATA_DIRS[@]}"; do
  if [ -d "$d" ]; then
    rm -rf "$d"
    echo -e "  ${GREEN}Removed${NC}: $d"
  else
    echo "  Skipped (not found): $d"
  fi
done

# ── Step 3: Purge images from containerd ─────────────────────────────────────
echo ""
echo "── Purging PatchPilot images from containerd ───────────────────────────"

# k3s uses its own containerd socket — prefer k3s crictl, fall back to crictl with socket flag
if command -v k3s &>/dev/null; then
  CRICTL="k3s crictl"
elif command -v crictl &>/dev/null; then
  # Point at the k3s containerd socket explicitly to avoid DeadlineExceeded
  CRICTL="crictl --runtime-endpoint unix:///run/k3s/containerd/containerd.sock"
else
  CRICTL=""
fi

if [ -n "$CRICTL" ]; then
  IMAGES=$($CRICTL images 2>/dev/null | grep "linit01/patchpilot" | awk '{print $3}' || true)
  if [ -n "$IMAGES" ]; then
    echo "$IMAGES" | while read -r img; do
      echo -n "  Removing image $img ... "
      if $CRICTL rmi "$img" 2>/dev/null; then
        echo -e "${GREEN}done${NC}"
      else
        echo -e "${YELLOW}skipped (may already be gone)${NC}"
      fi
    done
  else
    echo "  No PatchPilot images found in containerd cache."
  fi
else
  echo -e "  ${YELLOW}crictl not found — skipping image purge.${NC}"
  echo "  Run manually: sudo k3s crictl rmi \$(sudo k3s crictl images | grep patchpilot | awk '{print \$3}')"
fi

echo ""
echo -e "${GREEN}Done. Safe to re-run install-k3s.sh now.${NC}"
