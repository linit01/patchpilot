#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — build-push.sh
#
# Builds backend and frontend Docker images and pushes them to DockerHub.
# Run this on your Mac/workstation BEFORE running install-k3s.sh.
#
# Usage:
#   cd patchpilot
#   ./k8s/build-push.sh                  # reads tag/repo from install-config.yaml
#   ./k8s/build-push.sh --tag 0.9.5-alpha  # override tag
#   ./k8s/build-push.sh --platform linux/arm64  # override target platform
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/install-config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }
step() { echo ""; echo -e "${CYAN}▸${NC} $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

# ── YAML reader (same logic as install-k3s.sh) ────────────────────────────────
yaml_get() {
  local key="$1" default="${2:-}"
  if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
    python3 - "$key" "$default" "$CONFIG_FILE" <<'EOF'
import sys, yaml
key, default, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = yaml.safe_load(f)
    parts = key.split(".")
    val = data
    for p in parts:
        val = val[p]
    print("" if val is None else str(val))
except Exception:
    print(default)
EOF
  else
    # Fallback: naive grep
    local leaf="${key##*.}"
    grep -E "^\s+${leaf}:" "$CONFIG_FILE" | head -1 | sed "s/.*${leaf}:[[:space:]]*//" | tr -d "'\""
  fi
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
OVERRIDE_TAG=""
OVERRIDE_PLATFORM=""
NO_CACHE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)       OVERRIDE_TAG="$2";      shift 2 ;;
    --platform)  OVERRIDE_PLATFORM="$2"; shift 2 ;;
    --no-cache)  NO_CACHE="--no-cache";  shift ;;
    --help|-h)
      sed -n '/^# Usage/,/^# ─/p' "$0" | grep -v "^# ─"
      exit 0 ;;
    *) err "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
[[ -f "$CONFIG_FILE" ]] || { err "install-config.yaml not found at ${CONFIG_FILE}"; exit 1; }

DH_REPO="$(yaml_get patchpilot.image.dockerHubRepo linit01/patchpilot)"
DH_REPO="${DH_REPO%/}"
DH_USERNAME="$(yaml_get patchpilot.dockerHub.username)"
DH_TOKEN="$(yaml_get patchpilot.dockerHub.token)"
IMAGE_TAG="${OVERRIDE_TAG:-$(yaml_get patchpilot.image.tag 0.9.5-alpha)}"

# Detect cluster arch for platform if not overridden
if [[ -n "${OVERRIDE_PLATFORM}" ]]; then
  PLATFORM="${OVERRIDE_PLATFORM}"
elif command -v kubectl &>/dev/null && kubectl cluster-info &>/dev/null 2>&1; then
  NODE_ARCH="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}' 2>/dev/null || echo amd64)"
  case "${NODE_ARCH}" in
    amd64|x86_64)  PLATFORM="linux/amd64" ;;
    arm64|aarch64) PLATFORM="linux/arm64" ;;
    arm*)          PLATFORM="linux/arm/v7" ;;
    *)             PLATFORM="linux/amd64" ;;
  esac
else
  info "kubectl not available — defaulting to linux/amd64"
  PLATFORM="linux/amd64"
fi

ARCH="${PLATFORM##*/}"          # amd64, arm64, arm/v7
ARCH_SUFFIX="${ARCH//\//-}"     # amd64, arm64, arm-v7

BACKEND_IMAGE="${DH_REPO}:backend-${IMAGE_TAG}-${ARCH_SUFFIX}"
FRONTEND_IMAGE="${DH_REPO}:frontend-${IMAGE_TAG}-${ARCH_SUFFIX}"

HOST_PLATFORM="linux/$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
CROSS_BUILD=false
[[ "${PLATFORM}" != "${HOST_PLATFORM}" ]] && CROSS_BUILD=true

echo ""
echo -e "${CYAN}PatchPilot — Image Build & Push${NC}"
echo "  Repo     : ${DH_REPO}"
echo "  Tag      : ${IMAGE_TAG}"
echo "  Platform : ${PLATFORM}"
echo "  Backend  : ${BACKEND_IMAGE}"
echo "  Frontend : ${FRONTEND_IMAGE}"
echo "  Cross    : ${CROSS_BUILD}"
echo ""

# ── Prereq checks ─────────────────────────────────────────────────────────────
step "Checking prerequisites"
command -v docker &>/dev/null || { err "docker not found"; exit 1; }
if command -v timeout &>/dev/null; then
    timeout 10 docker info &>/dev/null 2>&1 || { err "Docker daemon not running"; exit 1; }
  else
    docker info &>/dev/null 2>&1 || { err "Docker daemon not running"; exit 1; }
  fi
ok "Docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo running)"

if [[ "${CROSS_BUILD}" == "true" ]]; then
  docker buildx version &>/dev/null || { err "docker buildx required for cross-arch builds"; exit 1; }
  ok "docker buildx: $(docker buildx version 2>/dev/null | awk '{print $2}' | head -1)"
fi

[[ -n "${DH_USERNAME}" ]] || { err "dockerHub.username not set in install-config.yaml"; exit 1; }
[[ -n "${DH_TOKEN}" ]]    || { err "dockerHub.token not set in install-config.yaml"; exit 1; }

# ── Docker Hub login ──────────────────────────────────────────────────────────
step "Logging in to Docker Hub"
echo "${DH_TOKEN}" | docker login --username "${DH_USERNAME}" --password-stdin
ok "Logged in as ${DH_USERNAME}"

# ── Build & push ──────────────────────────────────────────────────────────────
if [[ "${CROSS_BUILD}" == "true" ]]; then
  step "Cross-arch build via buildx (${HOST_PLATFORM} → ${PLATFORM})"

  if ! docker buildx inspect patchpilot-builder &>/dev/null; then
    docker buildx create --name patchpilot-builder --platform linux/amd64,linux/arm64 --use
  else
    docker buildx use patchpilot-builder
  fi

  info "Building + pushing backend..."
  docker buildx build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${BACKEND_IMAGE}" --push "${REPO_ROOT}"
  ok "Backend pushed: ${BACKEND_IMAGE}"

  info "Building + pushing frontend..."
  docker buildx build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile.frontend" \
    --tag "${FRONTEND_IMAGE}" --push "${REPO_ROOT}"
  ok "Frontend pushed: ${FRONTEND_IMAGE}"

else
  step "Same-arch build (${PLATFORM})"

  info "Building backend..."
  docker build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${BACKEND_IMAGE}" "${REPO_ROOT}"
  ok "Backend built: ${BACKEND_IMAGE}"

  info "Building frontend..."
  docker build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile.frontend" \
    --tag "${FRONTEND_IMAGE}" "${REPO_ROOT}"
  ok "Frontend built: ${FRONTEND_IMAGE}"

  step "Pushing images to Docker Hub"
  for img in "${BACKEND_IMAGE}" "${FRONTEND_IMAGE}"; do
    info "Pushing ${img}..."
    docker push "${img}"
    ok "Pushed: ${img}"
  done
fi

docker logout &>/dev/null || true

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Images pushed successfully!${NC}"
echo -e "${GREEN}  Backend : ${BACKEND_IMAGE}${NC}"
echo -e "${GREEN}  Frontend: ${FRONTEND_IMAGE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "Now run:  ${CYAN}./k8s/install-k3s.sh --no-interactive${NC}"
echo ""
