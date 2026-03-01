#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — build-push.sh
#
# Two modes:
#
#   RELEASE (--release)
#     Builds linux/amd64 + linux/arm64 in one shot via buildx and pushes a
#     multi-arch manifest list to Docker Hub under a CLEAN tag:
#       linit01/patchpilot:backend-0.9.5-alpha   ← what goes in install-config.yaml
#       linit01/patchpilot:frontend-0.9.5-alpha
#     Both arches live under that single tag; Docker/k8s pull the right one
#     automatically.  Also tags :backend-latest and :frontend-latest.
#
#   DEV (default)
#     Detects target cluster arch, builds for that arch only (faster), tags
#     with an arch suffix for clarity:
#       linit01/patchpilot:backend-0.9.5-alpha-amd64
#       linit01/patchpilot:frontend-0.9.5-alpha-amd64
#     Use --platform to override arch detection.
#
# Usage:
#   ./k8s/build-push.sh --release                    # public multi-arch release
#   ./k8s/build-push.sh --release --tag 0.9.7-alpha  # release with explicit tag
#   ./k8s/build-push.sh                              # dev: auto-detect cluster arch
#   ./k8s/build-push.sh --platform linux/arm64       # dev: force arch
#   ./k8s/build-push.sh --no-cache                   # force fresh layers
#   ./k8s/build-push.sh --no-push                    # build only, no push
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/install-config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; PURPLE='\033[0;35m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }
step() { echo ""; echo -e "${PURPLE}▸${NC} $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

# Cross-platform sed -i (macOS BSD vs Linux GNU)
sed_i() { sed -i '' "$@" 2>/dev/null || sed -i "$@"; }

# ── YAML reader ───────────────────────────────────────────────────────────────
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
    local leaf="${key##*.}"
    grep -E "^\s+${leaf}:" "$CONFIG_FILE" | head -1 \
      | sed "s/.*${leaf}:[[:space:]]*//" | tr -d "'\""
  fi
}

# ── Credential prompt helpers ──────────────────────────────────────────────────
_ensure_username() {
  [[ -n "${DH_USERNAME}" ]] && return

  echo ""
  warn "dockerHub.username is not set in install-config.yaml"
  echo -en "${CYAN}Enter your Docker Hub username: ${NC}"
  read -r DH_USERNAME
  [[ -n "${DH_USERNAME}" ]] || { err "No username entered — aborting."; exit 1; }

  echo -en "${CYAN}Save username to install-config.yaml for future runs? [y/N]: ${NC}"
  read -r save_choice
  if [[ "${save_choice:-N}" == [yY] ]]; then
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
      python3 - "${DH_USERNAME}" "${CONFIG_FILE}" <<'PYEOF'
import sys, yaml
username, path = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = yaml.safe_load(f)
data.setdefault("patchpilot", {}).setdefault("dockerHub", {})["username"] = username
with open(path, "w") as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
PYEOF
    else
      grep -q "username:" "${CONFIG_FILE}" \
        && sed_i "s|username:.*|username: ${DH_USERNAME}|" "${CONFIG_FILE}" \
        || echo "    username: ${DH_USERNAME}" >> "${CONFIG_FILE}"
    fi
    ok "Username saved."
  else
    info "Username not saved — you'll be prompted again next run."
  fi
}

_ensure_token() {
  [[ -n "${DH_TOKEN}" ]] && return

  echo ""
  warn "dockerHub.token is not set in install-config.yaml"
  echo -en "${CYAN}Enter your Docker Hub access token: ${NC}"
  read -rs DH_TOKEN
  echo ""
  [[ -n "${DH_TOKEN}" ]] || { err "No token entered — aborting."; exit 1; }

  echo -en "${CYAN}Save token to install-config.yaml for future runs? [y/N]: ${NC}"
  read -r save_choice
  if [[ "${save_choice:-N}" == [yY] ]]; then
    if command -v python3 &>/dev/null && python3 -c "import yaml" 2>/dev/null; then
      python3 - "${DH_TOKEN}" "${CONFIG_FILE}" <<'PYEOF'
import sys, yaml
token, path = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = yaml.safe_load(f)
data.setdefault("patchpilot", {}).setdefault("dockerHub", {})["token"] = token
with open(path, "w") as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
PYEOF
    else
      grep -q "token:" "${CONFIG_FILE}" \
        && sed_i "s|token:.*|token: ${DH_TOKEN}|" "${CONFIG_FILE}" \
        || echo "    token: ${DH_TOKEN}" >> "${CONFIG_FILE}"
    fi
    ok "Token saved."
  else
    info "Token not saved — you'll be prompted again next run."
  fi
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
RELEASE_MODE=false
OVERRIDE_TAG=""
OVERRIDE_PLATFORM=""
NO_CACHE=""
NO_PUSH=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)   RELEASE_MODE=true;      shift ;;
    --tag)       OVERRIDE_TAG="$2";      shift 2 ;;
    --platform)  OVERRIDE_PLATFORM="$2"; shift 2 ;;
    --no-cache)  NO_CACHE="--no-cache";  shift ;;
    --no-push)   NO_PUSH=true;           shift ;;
    --help|-h)
      sed -n '/^# Usage/,/^# ─/p' "$0" | grep -v "^# ─"
      exit 0 ;;
    *) err "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Load config ───────────────────────────────────────────────────────────────
[[ -f "$CONFIG_FILE" ]] || {
  err "install-config.yaml not found at ${CONFIG_FILE}"
  err "Copy the example: cp k8s/install-config.yaml.example k8s/install-config.yaml"
  exit 1
}

DH_REPO="$(yaml_get patchpilot.image.dockerHubRepo linit01/patchpilot)"
DH_REPO="${DH_REPO_OVERRIDE:-${DH_REPO}}"   # web UI / env override
DH_REPO="${DH_REPO%/}"
DH_USERNAME="$(yaml_get patchpilot.dockerHub.username)"
DH_TOKEN="$(yaml_get patchpilot.dockerHub.token)"
IMAGE_TAG="${OVERRIDE_TAG:-$(yaml_get patchpilot.image.tag 0.9.5-alpha)}"
HOST_PLATFORM="linux/$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"

# ─────────────────────────────────────────────────────────────────────────────
# MODE: RELEASE — multi-arch manifest list
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${RELEASE_MODE}" == "true" ]]; then

  RELEASE_PLATFORMS="linux/amd64,linux/arm64"
  BACKEND_IMAGE="${DH_REPO}:backend-${IMAGE_TAG}"
  FRONTEND_IMAGE="${DH_REPO}:frontend-${IMAGE_TAG}"
  BACKEND_LATEST="${DH_REPO}:backend-latest"
  FRONTEND_LATEST="${DH_REPO}:frontend-latest"

  echo ""
  echo -e "${PURPLE}PatchPilot — Multi-Arch RELEASE Build${NC}"
  echo "  Repo      : ${DH_REPO}"
  echo "  Tag       : ${IMAGE_TAG}"
  echo "  Platforms : ${RELEASE_PLATFORMS}"
  echo "  Backend   : ${BACKEND_IMAGE}  (+ :backend-latest)"
  echo "  Frontend  : ${FRONTEND_IMAGE}  (+ :frontend-latest)"
  echo ""
  warn "Builds both amd64 AND arm64 layers — first run takes ~10-15 min."
  echo ""

  step "Checking prerequisites"
  command -v docker &>/dev/null || { err "docker not found"; exit 1; }
  docker info &>/dev/null 2>&1   || { err "Docker daemon not running"; exit 1; }
  ok "Docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo running)"
  docker buildx version &>/dev/null || { err "docker buildx required for --release"; exit 1; }
  ok "docker buildx: $(docker buildx version 2>/dev/null | awk '{print $2}' | head -1)"
  _ensure_username

  # Ensure multi-arch builder exists
  if ! docker buildx inspect patchpilot-builder &>/dev/null; then
    info "Creating buildx builder (patchpilot-builder)..."
    docker buildx create --name patchpilot-builder \
      --platform linux/amd64,linux/arm64 \
      --driver-opt network=host \
      --use
  else
    docker buildx use patchpilot-builder
    ok "Using existing buildx builder"
  fi
  # Bootstrap (starts QEMU emulation for cross-arch)
  docker buildx inspect --bootstrap &>/dev/null
  ok "Builder bootstrapped"

  _ensure_token

  if [[ "${NO_PUSH}" == "false" ]]; then
    step "Logging in to Docker Hub"
    echo "${DH_TOKEN}" | docker login --username "${DH_USERNAME}" --password-stdin
    ok "Logged in as ${DH_USERNAME}"
  fi

  # buildx --push creates manifest list on Hub automatically.
  # --load cannot be used with multi-arch (local daemon only supports one platform).
  if [[ "${NO_PUSH}" == "true" ]]; then
    warn "--no-push with --release: can only load one platform locally."
    warn "Building amd64 only and loading to local daemon."
    BUILD_FLAGS="--platform linux/amd64 --load"
  else
    BUILD_FLAGS="--platform ${RELEASE_PLATFORMS} --push"
  fi

  step "Building backend"
  # shellcheck disable=SC2086
  docker buildx build ${NO_CACHE} ${BUILD_FLAGS} \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${BACKEND_IMAGE}" \
    --tag "${BACKEND_LATEST}" \
    "${REPO_ROOT}"
  ok "Backend: ${BACKEND_IMAGE}"

  step "Building frontend"
  # shellcheck disable=SC2086
  docker buildx build ${NO_CACHE} ${BUILD_FLAGS} \
    --file "${REPO_ROOT}/Dockerfile.frontend" \
    --tag "${FRONTEND_IMAGE}" \
    --tag "${FRONTEND_LATEST}" \
    "${REPO_ROOT}"
  ok "Frontend: ${FRONTEND_IMAGE}"

  [[ "${NO_PUSH}" == "false" ]] && { docker logout &>/dev/null || true; }

  echo ""
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}  Multi-arch release pushed to Docker Hub!${NC}"
  echo -e "${GREEN}  Backend : ${BACKEND_IMAGE}${NC}"
  echo -e "${GREEN}  Frontend: ${FRONTEND_IMAGE}${NC}"
  echo -e "${GREEN}  (:backend-latest and :frontend-latest also updated)${NC}"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  echo -e "Verify the manifest list:"
  echo -e "  ${CYAN}docker buildx imagetools inspect ${BACKEND_IMAGE}${NC}"
  echo -e "  → should list both linux/amd64 and linux/arm64 digests"
  echo ""
  echo -e "In install-config.yaml, use the CLEAN tag (no arch suffix):"
  echo -e "  ${CYAN}image:"
  echo -e "    dockerHubRepo: ${DH_REPO}"
  echo -e "    tag: ${IMAGE_TAG}${NC}"
  echo ""
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# MODE: DEV — single arch, fast iteration
# ─────────────────────────────────────────────────────────────────────────────

# Detect target arch
if [[ -n "${OVERRIDE_PLATFORM}" ]]; then
  PLATFORM="${OVERRIDE_PLATFORM}"
elif command -v kubectl &>/dev/null && kubectl cluster-info &>/dev/null 2>&1; then
  NODE_ARCH="$(kubectl get nodes \
    -o jsonpath='{.items[0].status.nodeInfo.architecture}' 2>/dev/null || echo amd64)"
  case "${NODE_ARCH}" in
    amd64|x86_64)  PLATFORM="linux/amd64" ;;
    arm64|aarch64) PLATFORM="linux/arm64" ;;
    arm*)          PLATFORM="linux/arm/v7" ;;
    *)             PLATFORM="linux/amd64" ;;
  esac
  info "Cluster arch detected: ${NODE_ARCH} → ${PLATFORM}"
else
  PLATFORM="linux/amd64"
  warn "kubectl not available — defaulting to linux/amd64 (use --platform to override)"
fi

ARCH="${PLATFORM##*/}"          # amd64, arm64, arm/v7
ARCH_SUFFIX="${ARCH//\//-}"     # amd64, arm64, arm-v7

BACKEND_IMAGE="${DH_REPO}:backend-${IMAGE_TAG}-${ARCH_SUFFIX}"
FRONTEND_IMAGE="${DH_REPO}:frontend-${IMAGE_TAG}-${ARCH_SUFFIX}"

CROSS_BUILD=false
[[ "${PLATFORM}" != "${HOST_PLATFORM}" ]] && CROSS_BUILD=true

echo ""
echo -e "${CYAN}PatchPilot — Dev Build${NC}"
echo "  Repo      : ${DH_REPO}"
echo "  Tag       : ${IMAGE_TAG}"
echo "  Platform  : ${PLATFORM}$([[ "${CROSS_BUILD}" == "true" ]] && echo ' (cross-build via buildx)' || echo ' (native)')"
echo "  Backend   : ${BACKEND_IMAGE}"
echo "  Frontend  : ${FRONTEND_IMAGE}"
echo ""
info "For a public multi-arch release, use:  ${CYAN}./k8s/build-push.sh --release${NC}"
echo ""

step "Checking prerequisites"
command -v docker &>/dev/null || { err "docker not found"; exit 1; }
docker info &>/dev/null 2>&1   || { err "Docker daemon not running"; exit 1; }
ok "Docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo running)"
[[ "${CROSS_BUILD}" == "true" ]] && {
  docker buildx version &>/dev/null || { err "docker buildx required for cross-arch builds"; exit 1; }
  ok "docker buildx available"
}
_ensure_username

_ensure_token

if [[ "${NO_PUSH}" == "false" ]]; then
  step "Logging in to Docker Hub"
  echo "${DH_TOKEN}" | docker login --username "${DH_USERNAME}" --password-stdin
  ok "Logged in as ${DH_USERNAME}"
fi

if [[ "${CROSS_BUILD}" == "true" ]]; then
  step "Cross-arch build via buildx (${HOST_PLATFORM} → ${PLATFORM})"

  if ! docker buildx inspect patchpilot-builder &>/dev/null; then
    docker buildx create --name patchpilot-builder \
      --platform linux/amd64,linux/arm64 --use
  else
    docker buildx use patchpilot-builder
  fi

  PUSH_FLAG="--push"
  [[ "${NO_PUSH}" == "true" ]] && PUSH_FLAG="--load"

  info "Building backend..."
  # shellcheck disable=SC2086
  docker buildx build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${BACKEND_IMAGE}" ${PUSH_FLAG} "${REPO_ROOT}"
  ok "Backend: ${BACKEND_IMAGE}"

  info "Building frontend..."
  # shellcheck disable=SC2086
  docker buildx build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile.frontend" \
    --tag "${FRONTEND_IMAGE}" ${PUSH_FLAG} "${REPO_ROOT}"
  ok "Frontend: ${FRONTEND_IMAGE}"

else
  step "Native build (${PLATFORM})"

  info "Building backend..."
  # shellcheck disable=SC2086
  docker build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile" \
    --tag "${BACKEND_IMAGE}" "${REPO_ROOT}"
  ok "Backend: ${BACKEND_IMAGE}"

  info "Building frontend..."
  # shellcheck disable=SC2086
  docker build ${NO_CACHE} --platform "${PLATFORM}" \
    --file "${REPO_ROOT}/Dockerfile.frontend" \
    --tag "${FRONTEND_IMAGE}" "${REPO_ROOT}"
  ok "Frontend: ${FRONTEND_IMAGE}"

  if [[ "${NO_PUSH}" == "false" ]]; then
    step "Pushing to Docker Hub"
    for img in "${BACKEND_IMAGE}" "${FRONTEND_IMAGE}"; do
      info "Pushing ${img}..."
      docker push "${img}"
      ok "Pushed: ${img}"
    done
  else
    info "--no-push: images in local Docker daemon only"
  fi
fi

[[ "${NO_PUSH}" == "false" ]] && { docker logout &>/dev/null || true; }

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Dev build complete!${NC}"
echo -e "${GREEN}  Backend : ${BACKEND_IMAGE}${NC}"
echo -e "${GREEN}  Frontend: ${FRONTEND_IMAGE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "Dev tag includes arch suffix: ${CYAN}${IMAGE_TAG}-${ARCH_SUFFIX}${NC}"
echo -e "Use ${CYAN}--release${NC} for public images with clean tags (no suffix)."
echo ""
echo -e "Now run:  ${CYAN}./k8s/install-k3s.sh --no-interactive${NC}"
echo ""
