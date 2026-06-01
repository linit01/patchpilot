#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — K3s Installer (version read from VERSION file)
#
# Usage:
#   ./k8s/install-k3s.sh                    # Uses k8s/install-config.yaml
#   ./k8s/install-k3s.sh --config my.yaml   # Custom config file
#   ./k8s/install-k3s.sh --interactive      # Force interactive prompts
#   ./k8s/install-k3s.sh --no-interactive   # Skip all prompts (web wizard mode)
#   ./k8s/install-k3s.sh --dry-run          # Generate manifests, don't apply
#   ./k8s/install-k3s.sh --uninstall        # Remove PatchPilot from cluster
#   ./k8s/install-k3s.sh --developer        # Dev mode: build+push private image then install
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── ERR trap — catch any unexpected exit and show exactly where ───────────────
# This fires whenever any command exits non-zero and set -e would kill the script.
# It tells us the exact line number and command that failed.
trap 'echo ""; echo "✗ INSTALLER DIED at line ${LINENO}: ${BASH_COMMAND}" >&2; echo "✗ Exit code: $?" >&2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/install-config.yaml"
GENERATED_DIR="${SCRIPT_DIR}/.generated"
VERSION_FILE="${REPO_ROOT}/VERSION"
PP_FILE_VERSION="$(cat "${VERSION_FILE}" 2>/dev/null | tr -d '[:space:]')"
PP_FILE_VERSION="${PP_FILE_VERSION:-0.0.0-dev}"
DRY_RUN=false
INTERACTIVE=false
NO_PROMPTS=false
UNINSTALL=false
PP_SC_WAIT_FOR_CONSUMER=false
DEVELOPER=false

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; PURPLE='\033[0;35m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }
step() { echo ""; echo -e "${PURPLE}▸${NC} $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
die()  { err "$*"; exit 1; }

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)         CONFIG_FILE="$2"; shift 2 ;;
    --config=*)       CONFIG_FILE="${1#--config=}"; shift ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --interactive)    INTERACTIVE=true; NO_PROMPTS=false; shift ;;
    --no-interactive) NO_PROMPTS=true; INTERACTIVE=false; shift ;;
    --uninstall)      UNINSTALL=true; shift ;;
    -h|--help)        sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

# Inherit NO_INTERACTIVE from parent install.sh if set
[[ "${NO_INTERACTIVE:-false}" == "true" ]] && NO_PROMPTS=true

# ── Banner ────────────────────────────────────────────────────────────────────
print_banner() {
  echo -e "${PURPLE}"
  cat << "EOF"
    ____        __       __    ____  _ __      __
   / __ \____ _/ /______/ /_  / __ \(_) /___  / /_
  / /_/ / __ `/ __/ ___/ __ \/ /_/ / / / __ \/ __/
 / ____/ /_/ / /_/ /__/ / / / ____/ / / /_/ / /_
/_/    \__,_/\__/\___/_/ /_/_/   /_/_/\____/\__/
EOF
  echo -e "${NC}"
  echo -e "${BLUE}K3s Installer — v${PP_FILE_VERSION}${NC}"
  echo ""
}

# ── YAML reader ────────────────────────────────────────────────────────────────
yaml_get() {
  local key="$1" default="${2:-}"
  python3 - "${CONFIG_FILE}" "${key}" "${default}" << 'PYEOF'
import sys, re
def get_nested(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d
def parse(filepath):
    try:
        import yaml
        with open(filepath) as f:
            return yaml.safe_load(f)
    except ImportError:
        pass
    result = {}
    with open(filepath) as f:
        for line in f:
            m = re.match(r'^(\s*)(\w[\w-]*):\s*(.*)', line.rstrip())
            if m:
                result[m.group(2)] = m.group(3).strip().strip('"\'') or None
    return result
filepath, keypath, default = sys.argv[1], sys.argv[2].split('.'), sys.argv[3] if len(sys.argv) > 3 else ''
try:
    val = get_nested(parse(filepath), keypath)
    if val is None or val == '' or val == 'null':
        print(default)
    elif isinstance(val, bool):
        print('true' if val else 'false')
    elif isinstance(val, list):
        print('\n'.join(str(x) for x in val))
    else:
        print(str(val))
except Exception:
    print(default)
PYEOF
}

gen_password()   { python3 -c "import secrets, string; c=string.ascii_letters+string.digits+'-_=+'; print(''.join(secrets.choice(c) for _ in range(32)))"; }
gen_fernet_key() { python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

# prompt_value: in NO_PROMPTS mode always returns the current/default value
prompt_value() {
  local desc="$1" current="$2" required="${3:-false}"
  if [[ "${NO_PROMPTS}" == "true" ]]; then
    echo "${current}"
    return
  fi
  if [[ "${INTERACTIVE}" == "false" && -n "${current}" ]]; then
    echo "${current}"; return
  fi
  local prompt_str="${CYAN}${desc}${NC}"
  [[ -n "${current}" ]] && prompt_str+=" [${current}]"
  [[ "${required}" == "true" && -z "${current}" ]] && prompt_str+=" ${RED}(required)${NC}"
  prompt_str+=": "
  local value=""
  while true; do
    echo -en "${prompt_str}" >&2; read -r value
    [[ -z "${value}" ]] && value="${current}"
    [[ -z "${value}" && "${required}" == "true" ]] && { echo -e "${RED}Required.${NC}" >&2; continue; }
    break
  done
  echo "${value}"
}

# detect_default_sc: returns the cluster's default StorageClass (the one
# annotated storageclass.kubernetes.io/is-default-class=true). Falls back to
# 'local-path' (the stock k3s provisioner) if none is marked default or kubectl
# can't reach the cluster. Result is memoized in PP_DETECTED_DEFAULT_SC.
detect_default_sc() {
  if [[ -n "${PP_DETECTED_DEFAULT_SC:-}" ]]; then
    echo "${PP_DETECTED_DEFAULT_SC}"; return
  fi
  local sc
  sc="$(kubectl get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1)"
  [[ -z "${sc}" ]] && sc="local-path"
  PP_DETECTED_DEFAULT_SC="${sc}"
  echo "${sc}"
}

# _existing_secret_value KEY — print the decoded value of a key in the existing
# patchpilot-secrets Secret, or empty if the secret/key is absent. A re-run must
# REUSE the password and Fernet key already in the cluster, because:
#   • Postgres only honours POSTGRES_PASSWORD on first init — a regenerated
#     password will not match the already-initialised data volume, so the
#     backend fails with "password authentication failed".
#   • The Fernet key decrypts stored SSH secrets — regenerating it makes every
#     stored credential undecryptable.
# Decode with python3 (always present; portable across macOS/Linux base64).
_existing_secret_value() {
  local key="$1" b64
  b64="$(kubectl get secret patchpilot-secrets -n "${PP_NAMESPACE}" \
    -o jsonpath="{.data.${key}}" 2>/dev/null || true)"
  [[ -z "${b64}" ]] && return 0
  printf '%s' "${b64}" \
    | python3 -c "import sys,base64; sys.stdout.write(base64.b64decode(sys.stdin.read()).decode())" 2>/dev/null \
    || true
}

# _pick_ip: from a space-separated list of addresses return a single one,
# preferring IPv4. Dual-stack nodes report both an IPv4 and an IPv6 InternalIP;
# concatenating them produces a broken ssh target like
# "ssh 10.0.10.101 fd06:...:bb52 sudo rm -rf ...".
_pick_ip() {
  local addrs="$1" ip
  ip="$(printf '%s\n' ${addrs} | grep -m1 -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || true)"
  [[ -z "${ip}" ]] && ip="$(printf '%s\n' ${addrs} | head -1)"
  printf '%s' "${ip}"
}

# confirm_proceed: auto-yes in NO_PROMPTS mode
confirm_proceed() {
  local msg="$1"
  if [[ "${NO_PROMPTS}" == "true" ]]; then return 0; fi
  echo -en "${YELLOW}${msg} [y/N] ${NC}"; read -r c
  [[ "${c}" =~ ^[Yy]$ ]]
}

# ── Uninstall ──────────────────────────────────────────────────────────────────
do_uninstall() {
  step "Uninstalling PatchPilot from cluster"
  local ns; ns="$(yaml_get patchpilot.namespace patchpilot)"
  local data_dir; data_dir="$(yaml_get patchpilot.storage.dataDir /app-data)"
  data_dir="${data_dir%/}"
  warn "This will delete namespace '${ns}' and ALL data within it."

  if [[ "${NO_PROMPTS}" == "true" ]]; then
    info "Auto-confirmed via web installer"
  else
    echo -en "${RED}Type namespace to confirm [${ns}]: ${NC}"; read -r confirm
    [[ "${confirm}" != "${ns}" ]] && { info "Cancelled."; exit 0; }
  fi

  # ── Step 1: Clean hostPath dirs via privileged in-cluster Job ─────────────
  # Runs a busybox container that mounts the data directory from the node
  # directly — no SSH required. Must run BEFORE namespace deletion so the
  # Job has a namespace to live in.
  #
  # Only needed for hostpath-mode installs (static PVs named patchpilot-*).
  # Dynamic volumes have no data under ${data_dir}; their provisioner reclaims
  # them when the namespace/PVCs are deleted, so the Job would only create an
  # empty ${data_dir} on the node for nothing — skip it.
  local has_static_pv=false
  if kubectl get pv patchpilot-postgres-data &>/dev/null || \
     kubectl get pv patchpilot-ansible-data &>/dev/null; then
    has_static_pv=true
  fi
  step "Running hostPath cleanup Job (no SSH required)"
  local job_name="patchpilot-hostpath-cleanup"
  local job_json
  job_json=$(cat <<JOBEOF
{
  "apiVersion": "batch/v1",
  "kind": "Job",
  "metadata": { "name": "${job_name}", "namespace": "${ns}" },
  "spec": {
    "backoffLimit": 0,
    "ttlSecondsAfterFinished": 30,
    "template": {
      "spec": {
        "restartPolicy": "Never",
        "tolerations": [{ "operator": "Exists" }],
        "containers": [{
          "name": "cleanup",
          "image": "busybox:1.36",
          "command": ["sh", "-c",
            "find /data-dir -maxdepth 1 -name 'patchpilot-*' ! -name 'patchpilot-backups' -exec rm -rf {} + 2>/dev/null; echo done"],
          "securityContext": { "runAsUser": 0 },
          "volumeMounts": [{ "name": "data-dir", "mountPath": "/data-dir" }]
        }],
        "volumes": [{
          "name": "data-dir",
          "hostPath": { "path": "${data_dir}", "type": "DirectoryOrCreate" }
        }]
      }
    }
  }
}
JOBEOF
)

  local job_succeeded=false
  # Delete any leftover job from a prior attempt
  kubectl delete job "${job_name}" -n "${ns}" --ignore-not-found=true &>/dev/null

  if [[ "${has_static_pv}" != "true" ]]; then
    ok "Dynamic volumes — provisioner reclaims data on namespace deletion; skipping node cleanup Job"
    job_succeeded=true
  elif echo "${job_json}" | kubectl apply -f - &>/dev/null; then
    info "Cleanup Job created — waiting up to 60s..."
    if kubectl wait --for=condition=complete "job/${job_name}"         -n "${ns}" --timeout=60s &>/dev/null; then
      ok "hostPath cleanup complete"
      job_succeeded=true
    else
      warn "Cleanup Job did not complete in 60s — data may remain on node"
    fi
  else
    warn "Could not create cleanup Job — namespace may already be gone"
  fi

  # ── Step 2: Delete namespace ───────────────────────────────────────────────
  # postgres-data and ansible-data PVs use reclaimPolicy: Delete — removed automatically.
  # patchpilot-backups PV uses reclaimPolicy: Retain — backup archives survive uninstall
  # and remain at ${data_dir}/patchpilot-backups for post-uninstall restore.
  info "Deleting namespace ${ns}..."
  kubectl delete namespace "${ns}" --ignore-not-found=true

  # ── Step 3: Delete ClusterIssuer ──────────────────────────────────────────
  local issuer; issuer="$(yaml_get patchpilot.network.tls.clusterIssuer letsencrypt-prod)"
  kubectl delete clusterissuer "${issuer}" --ignore-not-found=true 2>/dev/null || true

  # ── Step 6: Surface fallback/manual commands ───────────────────────────────
  local node_ip
  node_ip="$(_pick_ip "$(kubectl get nodes \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' \
    2>/dev/null)")"
  local node_ref="${node_ip:-<k3s-node-ip>}"

  if [[ "${job_succeeded}" == "false" ]]; then
    warn "hostPath cleanup Job did not complete — run on the k3s node:"
    warn "  ssh ${node_ref} 'sudo rm -rf ${data_dir}/patchpilot-*'"
    echo "__NOTE_CLEANUP__ ssh ${node_ref} 'sudo rm -rf ${data_dir}/patchpilot-*'"
  fi

  # ── Step 4: Remove PatchPilot images from containerd via SSH ─────────────
  if [[ -n "${node_ip}" ]]; then
    info "Removing PatchPilot images from k3s containerd cache..."
    local crictl_cmd="sudo k3s crictl rmi \$(sudo k3s crictl images | grep patchpilot | awk '{print \$3}') 2>/dev/null || true"
    if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes         "${node_ip}" "${crictl_cmd}" 2>/dev/null; then
      ok "PatchPilot images removed from containerd"
    else
      warn "Could not remove images via SSH — run manually on node:"
      warn "  ssh ${node_ref} \"${crictl_cmd}\""
      echo "__NOTE_CLEANUP__ ssh ${node_ref} \"${crictl_cmd}\""
    fi
  else
    warn "Could not determine node IP — remove images manually:"
    warn "  ssh <k3s-node-ip> \"sudo k3s crictl rmi \$(sudo k3s crictl images | grep patchpilot | awk '{print \$3}') 2>/dev/null || true\""
  fi

  ok "PatchPilot uninstalled."
  exit 0
}

# ── Prerequisites ─────────────────────────────────────────────────────────────
check_prerequisites() {
  step "Checking prerequisites"
  command -v kubectl &>/dev/null || die "kubectl not found"
  ok "kubectl: $(kubectl version --client --short 2>/dev/null | head -1)"
  command -v python3 &>/dev/null || die "python3 not found"
  ok "python3: $(python3 --version)"
  python3 -c "import yaml" 2>/dev/null && ok "PyYAML available" \
    || warn "PyYAML not installed — using fallback parser (pip3 install pyyaml recommended)"
  if command -v envsubst &>/dev/null; then
    ok "envsubst: $(command -v envsubst)"
  else
    err "envsubst not found — required to render manifests"
    if [[ "$(uname -s)" == "Darwin" ]]; then
      err "On macOS, install via Homebrew: brew install gettext"
      err "gettext is keg-only, so envsubst may not be on PATH. If it still"
      err "isn't found after install, run: brew link --force gettext"
      err "(or add \$(brew --prefix gettext)/bin to PATH)"
    else
      err "Install it via your package manager (e.g. apt-get install gettext-base)"
    fi
    die "Missing required dependency: envsubst"
  fi
  # Docker is only required when building and pushing images locally.
  # In registry mode (images pre-pushed to DockerHub) or no-interactive/web
  # wizard mode, skip Docker checks to avoid hanging if Docker daemon isn't
  # running on the deploy machine.
  local _strategy
  _strategy="$(yaml_get patchpilot.image.strategy registry)"
  if [[ "${_strategy}" != "registry" ]] && [[ "${NO_PROMPTS}" != "true" ]]; then
    command -v docker &>/dev/null || die "Docker not found"
    if command -v timeout &>/dev/null; then
      timeout 10 docker info &>/dev/null 2>&1 || die "Docker not running (or timed out)"
    else
      docker info &>/dev/null 2>&1 || die "Docker daemon not running"
    fi
    ok "Docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'running')"
    docker buildx version &>/dev/null || die "docker buildx not found"
    ok "docker buildx: $(docker buildx version 2>/dev/null | awk '{print $2}' | head -1)"
  else
    ok "Docker checks skipped (strategy=${_strategy}, images pulled from registry)"
  fi
  kubectl cluster-info &>/dev/null || die "Cannot reach Kubernetes cluster — check KUBECONFIG"
  ok "Cluster: $(kubectl config current-context)"
  [[ -f "${CONFIG_FILE}" ]] || die "Config not found: ${CONFIG_FILE}"
  ok "Config: ${CONFIG_FILE}"
}

# ── Detect target platform ─────────────────────────────────────────────────────
detect_target_platform() {
  local node_arch
  node_arch="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}' 2>/dev/null)"
  case "${node_arch}" in
    amd64|x86_64)  echo "linux/amd64" ;;
    arm64|aarch64) echo "linux/arm64" ;;
    arm*)          echo "linux/arm/v7" ;;
    *)             warn "Unknown node arch '${node_arch}' — defaulting to linux/amd64"; echo "linux/amd64" ;;
  esac
}

# ── Load configuration ─────────────────────────────────────────────────────────
load_config() {
  step "Loading configuration"

  PP_VERSION="$(yaml_get patchpilot.version "${PP_FILE_VERSION}")"
  PP_NAMESPACE="$(yaml_get patchpilot.namespace patchpilot)"
  PP_NAMESPACE="$(prompt_value "Kubernetes namespace" "${PP_NAMESPACE}")"

  # ── Image config — arch-suffixed tags to prevent arm/amd64 collision ─────
  PP_DH_REPO="$(yaml_get patchpilot.image.dockerHubRepo linit01/patchpilot)"
  PP_DH_REPO="$(prompt_value "Docker Hub repo" "${PP_DH_REPO}" true)"
  PP_DH_REPO="${PP_DH_REPO%/}"

  PP_IMAGE_STRATEGY="$(yaml_get patchpilot.image.strategy registry)"
  PP_IMAGE_TAG="$(yaml_get patchpilot.image.tag "${PP_FILE_VERSION}")"
  PP_IMAGE_PULL_POLICY="$(yaml_get patchpilot.image.pullPolicy IfNotPresent)"
  PP_PULL_SECRET_NAME="$(yaml_get patchpilot.image.pullSecretName dockerhub-pull-secret)"

  # Detect cluster arch (used by developer mode for single-arch local builds)
  local target_platform
  target_platform="$(detect_target_platform)"
  PP_TARGET_ARCH="$(echo "${target_platform}" | sed 's|linux/||;s|/|-|')"  # amd64, arm64, arm-v7
  info "Cluster architecture: ${PP_TARGET_ARCH}"

  if [[ "${DEVELOPER}" == "true" ]]; then
    # Developer mode: arch-suffixed tags for local single-arch builds
    # e.g. linit01/patchpilot:backend-0.9.8-alpha-amd64
    PP_BACKEND_IMAGE="${PP_DH_REPO}:backend-${PP_IMAGE_TAG}-${PP_TARGET_ARCH}"
    PP_FRONTEND_IMAGE="${PP_DH_REPO}:frontend-${PP_IMAGE_TAG}-${PP_TARGET_ARCH}"
  else
    # Registry mode: CI pushes multi-arch manifests without arch suffix
    # e.g. linit01/patchpilot:backend-0.9.8-alpha
    PP_BACKEND_IMAGE="${PP_DH_REPO}:backend-${PP_IMAGE_TAG}"
    PP_FRONTEND_IMAGE="${PP_DH_REPO}:frontend-${PP_IMAGE_TAG}"
  fi
  info "Backend image:  ${PP_BACKEND_IMAGE}"
  info "Frontend image: ${PP_FRONTEND_IMAGE}"

  # ── Docker Hub credentials ─────────────────────────────────────────────────
  PP_DH_USERNAME="$(yaml_get patchpilot.dockerHub.username)"
  PP_DH_TOKEN="$(yaml_get patchpilot.dockerHub.token)"

  if [[ "${DEVELOPER}" == "true" ]]; then
    # Developer mode — credentials required for build+push to private repo
    if [[ -z "${PP_DH_USERNAME}" ]]; then
      echo ""
      echo -e "${CYAN}Docker Hub credentials${NC}"
      echo -e "  Use an Access Token (not your password): hub.docker.com → Account Settings → Security"
      echo ""
      echo -en "${CYAN}  Docker Hub username${NC}: "; read -r PP_DH_USERNAME
    fi
    if [[ -z "${PP_DH_TOKEN}" ]]; then
      echo -en "${CYAN}  Docker Hub access token${NC}: "; read -rs PP_DH_TOKEN; echo ""
    fi
    [[ -z "${PP_DH_USERNAME}" || -z "${PP_DH_TOKEN}" ]] && die "Docker Hub credentials required in --developer mode."
  else
    # Standard mode — public image, no credentials needed for pull
    # A pull secret is still created if credentials are present in install-config.yaml
    # but installation proceeds without them for public images
    if [[ -n "${PP_DH_USERNAME}" && -n "${PP_DH_TOKEN}" ]]; then
      info "Docker Hub credentials found — will create pull secret."
    else
      info "No Docker Hub credentials — using public image (no pull secret needed)."
      PP_DH_USERNAME=""
      PP_DH_TOKEN=""
    fi
  fi

  # ── Network ────────────────────────────────────────────────────────────────
  PP_HOSTNAME="$(yaml_get patchpilot.network.hostname)"
  PP_HOSTNAME="$(prompt_value "Primary hostname" "${PP_HOSTNAME}" true)"

  PP_ADDITIONAL_HOSTNAMES_RAW="$(yaml_get patchpilot.network.additionalHostnames)"
  if [[ "${INTERACTIVE}" == "true" && "${NO_PROMPTS}" != "true" ]]; then
    echo -e "${CYAN}Additional hostnames (space-separated, blank=none):${NC}" >&2
    read -r extra; PP_ADDITIONAL_HOSTNAMES_RAW="${extra}"
  fi
  PP_ADDITIONAL_HOSTNAMES=()
  if [[ -n "${PP_ADDITIONAL_HOSTNAMES_RAW}" ]]; then
    while IFS= read -r _h; do
      [[ -n "${_h}" ]] && PP_ADDITIONAL_HOSTNAMES+=("${_h}")
    done <<< "$(echo "${PP_ADDITIONAL_HOSTNAMES_RAW}" | tr ' ,' '\n')"
  fi
  ALL_HOSTNAMES=("${PP_HOSTNAME}")
  for h in "${PP_ADDITIONAL_HOSTNAMES[@]:-}"; do [[ -n "${h}" ]] && ALL_HOSTNAMES+=("${h}"); done

  PP_TLS_ENABLED="$(yaml_get patchpilot.network.tls.enabled true)"
  # tls.mode: acme (default — cert-manager + ClusterIssuer/ACME) or byo (bring
  # your own — reference a pre-created TLS secret, no cert-manager). byo is the
  # path for internal/private hostnames (e.g. *.apps.lan) that public ACME CAs
  # cannot validate.
  PP_TLS_MODE="$(yaml_get patchpilot.network.tls.mode acme)"
  PP_TLS_MODE="$(printf '%s' "${PP_TLS_MODE}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${PP_TLS_MODE}" != "acme" && "${PP_TLS_MODE}" != "byo" ]]; then
    warn "Unknown tls.mode '${PP_TLS_MODE}' — defaulting to 'acme'"
    PP_TLS_MODE="acme"
  fi
  PP_CLUSTER_ISSUER="$(yaml_get patchpilot.network.tls.clusterIssuer letsencrypt-prod)"
  PP_TLS_SECRET_NAME="$(yaml_get patchpilot.network.tls.secretName)"
  PP_HTTPS_REDIRECT="$(yaml_get patchpilot.network.httpsRedirect true)"
  PP_SECURITY_HEADERS="$(yaml_get patchpilot.network.securityHeaders true)"
  PP_INGRESS_CLASS="$(yaml_get patchpilot.network.ingressClass traefik)"
  [[ -z "${PP_TLS_SECRET_NAME}" ]] && PP_TLS_SECRET_NAME="$(echo "${PP_HOSTNAME}" | tr '.' '-')-tls"

  [[ "${PP_TLS_ENABLED}" == "true" ]] && PP_BASE_URL="https://${PP_HOSTNAME}" || PP_BASE_URL="http://${PP_HOSTNAME}"

  local origins=()
  for h in "${ALL_HOSTNAMES[@]}"; do
    [[ "${PP_TLS_ENABLED}" == "true" ]] && origins+=("https://${h}") || origins+=("http://${h}")
  done
  PP_ALLOWED_ORIGINS="$(IFS=','; echo "${origins[*]}")"

  # ── cert-manager ───────────────────────────────────────────────────────────
  PP_CREATE_CLUSTER_ISSUER="$(yaml_get patchpilot.certManager.createClusterIssuer true)"
  PP_LE_EMAIL="$(yaml_get patchpilot.certManager.email)"
  PP_CHALLENGE_TYPE="$(yaml_get patchpilot.certManager.challengeType dns01-cloudflare)"
  PP_CF_EMAIL="$(yaml_get patchpilot.certManager.cloudflare.email)"
  PP_CF_API_TOKEN_SECRET="$(yaml_get patchpilot.certManager.cloudflare.apiTokenSecretName cloudflare-api-token-secret)"
  if [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "acme" ]]; then
    PP_LE_EMAIL="$(prompt_value "Let's Encrypt email" "${PP_LE_EMAIL}" true)"
    [[ "${PP_CHALLENGE_TYPE}" == "dns01-cloudflare" ]] && \
      PP_CF_EMAIL="$(prompt_value "Cloudflare account email" "${PP_CF_EMAIL}" true)"
  elif [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "byo" ]]; then
    info "TLS mode: bring-your-own — referencing pre-created secret '${PP_TLS_SECRET_NAME}' (no cert-manager/ACME)"
  fi

  # ── Database ───────────────────────────────────────────────────────────────
  PP_DB_USER="$(yaml_get patchpilot.postgres.user patchpilot)"
  PP_DB_PASSWORD="$(yaml_get patchpilot.postgres.password)"
  PP_DB_NAME="$(yaml_get patchpilot.postgres.database patchpilot)"
  PP_POSTGRES_STORAGE_SIZE="$(yaml_get patchpilot.postgres.storageSize 5Gi)"
  PP_POSTGRES_STORAGE_CLASS="$(yaml_get patchpilot.postgres.storageClass "$(detect_default_sc)")"
  if [[ -z "${PP_DB_PASSWORD}" ]]; then
    # Reuse the password already in the cluster so a re-run matches the
    # initialised Postgres data volume (Postgres ignores POSTGRES_PASSWORD after
    # first init). Only generate a fresh one when no secret exists yet.
    PP_DB_PASSWORD="$(_existing_secret_value POSTGRES_PASSWORD)"
    if [[ -n "${PP_DB_PASSWORD}" ]]; then
      info "Reusing existing PostgreSQL password from patchpilot-secrets (matches the initialised data volume)"
    else
      PP_DB_PASSWORD="$(gen_password)"
      warn "Auto-generated PostgreSQL password: ${YELLOW}${PP_DB_PASSWORD}${NC} — save this"
    fi
  fi

  # ── Encryption key ─────────────────────────────────────────────────────────
  PP_ENCRYPTION_KEY="$(yaml_get patchpilot.app.encryptionKey)"
  if [[ -z "${PP_ENCRYPTION_KEY}" ]]; then
    # Reuse the existing Fernet key — regenerating it would make every stored
    # SSH credential undecryptable. Only generate when none exists.
    PP_ENCRYPTION_KEY="$(_existing_secret_value PATCHPILOT_ENCRYPTION_KEY)"
    if [[ -n "${PP_ENCRYPTION_KEY}" ]]; then
      info "Reusing existing encryption key from patchpilot-secrets (keeps stored SSH secrets decryptable)"
    else
      PP_ENCRYPTION_KEY="$(gen_fernet_key)"
      warn "Auto-generated Fernet key: ${YELLOW}${PP_ENCRYPTION_KEY}${NC} — save this"
    fi
  fi

  # ── Application ────────────────────────────────────────────────────────────
  PP_AUTO_REFRESH_INTERVAL="$(yaml_get patchpilot.app.autoRefreshInterval 300)"
  PP_DEFAULT_SSH_USER="$(yaml_get patchpilot.app.defaultSshUser root)"
  PP_DEFAULT_SSH_PORT="$(yaml_get patchpilot.app.defaultSshPort 22)"
  PP_BACKUP_RETAIN_COUNT="$(yaml_get patchpilot.app.backupRetainCount 10)"
  PP_MAX_BACKUP_SIZE_MB="$(yaml_get patchpilot.app.maxBackupSizeMb 500)"

  # ── Storage ────────────────────────────────────────────────────────────────
  echo ""
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo -e "  Storage Configuration"
  echo -e "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  echo -e "${BLUE}Available StorageClasses in cluster:${NC}"
  kubectl get sc --no-headers 2>/dev/null | awk '{printf "  %-25s %s\n", $1, $2}' || echo "  (none found)"
  echo ""

  # Postgres — always local
  PP_POSTGRES_STORAGE_CLASS="$(yaml_get patchpilot.postgres.storageClass "$(detect_default_sc)")"
  PP_POSTGRES_STORAGE_CLASS="$(prompt_value "PostgreSQL StorageClass (local recommended)" "${PP_POSTGRES_STORAGE_CLASS}" true)"
  PP_POSTGRES_STORAGE_SIZE="$(yaml_get patchpilot.postgres.storageSize 5Gi)"
  PP_POSTGRES_STORAGE_SIZE="$(prompt_value "PostgreSQL volume size" "${PP_POSTGRES_STORAGE_SIZE}")"
  PP_POSTGRES_STORAGE_CLASS_SPEC="storageClassName: ${PP_POSTGRES_STORAGE_CLASS}"
  echo ""

  # ── Backup storage — ask intent FIRST, no forced NFS ─────────────────────
  PP_BACKUP_STORAGE_TYPE="$(yaml_get patchpilot.storage.type local)"

  if [[ "${NO_PROMPTS}" != "true" && "${INTERACTIVE}" != "false" ]] || \
     [[ "${PP_BACKUP_STORAGE_TYPE}" == "" ]]; then
    echo -e "${CYAN}Where should PatchPilot store backups?${NC}"
    echo "  1) Local disk on the k3s node  (simpler, faster, node-bound)"
    echo "  2) NFS share                   (survives node failure, TrueNAS/Synology)"
    echo ""
    local _schoice=""
    while [[ "${_schoice}" != "1" && "${_schoice}" != "2" ]]; do
      echo -en "${CYAN}Choose [1/2, default=1]: ${NC}"; read -r _schoice
      [[ -z "${_schoice}" ]] && _schoice="1"
    done
    [[ "${_schoice}" == "2" ]] && PP_BACKUP_STORAGE_TYPE="nfs" || PP_BACKUP_STORAGE_TYPE="local"
  fi

  if [[ "${PP_BACKUP_STORAGE_TYPE}" == "nfs" ]]; then
    PP_APP_STORAGE_CLASS="$(yaml_get patchpilot.storage.storageClass nfs-backups)"
    PP_APP_STORAGE_CLASS="$(prompt_value "Backups StorageClass (NFS)" "${PP_APP_STORAGE_CLASS}" true)"
    PP_NFS_SERVER="$(yaml_get patchpilot.storage.nfsServer)"
    PP_NFS_SERVER="$(prompt_value "NFS server IP" "${PP_NFS_SERVER}" true)"
    PP_NFS_SHARE="$(yaml_get patchpilot.storage.nfsShare)"
    PP_NFS_SHARE="$(prompt_value "NFS export path (e.g. /mnt/nas1/BACKUPS)" "${PP_NFS_SHARE}" true)"
    PP_NFS_SHARE="${PP_NFS_SHARE%/}"
    info "Using NFS: ${PP_NFS_SERVER}:${PP_NFS_SHARE}"
  else
    PP_APP_STORAGE_CLASS="${PP_POSTGRES_STORAGE_CLASS}"
    PP_NFS_SERVER=""
    PP_NFS_SHARE=""
    info "Using local disk for backups (StorageClass: ${PP_APP_STORAGE_CLASS})"
  fi

  PP_BACKUPS_STORAGE_SIZE="$(yaml_get patchpilot.storage.backupsSize 10Gi)"
  PP_BACKUPS_STORAGE_SIZE="$(prompt_value "Backups volume size" "${PP_BACKUPS_STORAGE_SIZE}")"
  PP_ANSIBLE_STORAGE_SIZE="$(yaml_get patchpilot.storage.ansibleSize 1Gi)"
  PP_ANSIBLE_STORAGE_SIZE="$(prompt_value "Ansible volume size" "${PP_ANSIBLE_STORAGE_SIZE}")"
  PP_APP_STORAGE_CLASS_SPEC="storageClassName: ${PP_APP_STORAGE_CLASS}"

  # Ansible always uses local (same as postgres)
  PP_ANSIBLE_STORAGE_CLASS="${PP_POSTGRES_STORAGE_CLASS}"
  PP_ANSIBLE_STORAGE_CLASS_SPEC="storageClassName: ${PP_ANSIBLE_STORAGE_CLASS}"

  # ── Data directory on node — parent directory for all hostPath PVs ─────────
  PP_DATA_DIR="$(yaml_get patchpilot.storage.dataDir /app-data)"
  PP_DATA_DIR="${PP_DATA_DIR%/}"  # strip trailing slash

  # ── Decide provisioning mode per volume ────────────────────────────────────
  # dynamic   — the StorageClass has a real dynamic provisioner (e.g.
  #             rancher.io/local-path). Emit a PVC only and let the provisioner
  #             create the volume in its own directory. No static PV, no
  #             hostPath, no /app-data, no node pinning.
  # hostpath  — no StorageClass, or a no-provisioner SC. Emit a static hostPath
  #             PV under ${PP_DATA_DIR} pinned to the node (legacy behaviour).
  # nfs       — backups on an NFS share: emit a static NFS PV (unchanged).
  _local_mode() {
    local sc="$1" prov
    [[ -z "${sc}" ]] && { echo "hostpath"; return; }
    prov="$(kubectl get sc "${sc}" -o jsonpath='{.provisioner}' 2>/dev/null || true)"
    case "${prov}" in
      ""|kubernetes.io/no-provisioner) echo "hostpath" ;;
      *)                               echo "dynamic"  ;;
    esac
  }

  # _pv_doc: full PersistentVolume YAML document for static modes, or empty
  # string for dynamic mode (PVC-only). Args: name component size reclaim sc mode
  _pv_doc() {
    local name="$1" comp="$2" size="$3" reclaim="$4" sc="$5" mode="$6"
    [[ "${mode}" == "dynamic" ]] && { printf ''; return; }
    local src
    if [[ "${mode}" == "nfs" ]]; then
      src="$(printf '  mountOptions:\n  - nfsvers=3\n  - hard\n  nfs:\n    server: %s\n    path: %s' \
        "${PP_NFS_SERVER}" "${PP_NFS_SHARE}")"
    else
      local node
      node="$(kubectl get nodes --field-selector='spec.unschedulable!=true' \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
      src="$(printf '  hostPath:\n    path: %s/%s\n    type: DirectoryOrCreate\n  nodeAffinity:\n    required:\n      nodeSelectorTerms:\n      - matchExpressions:\n        - key: kubernetes.io/hostname\n          operator: In\n          values:\n          - %s' \
        "${PP_DATA_DIR}" "${name}" "${node}")"
    fi
    cat <<PVDOC
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${name}
  labels:
    app.kubernetes.io/name: patchpilot
    app.kubernetes.io/component: ${comp}
    app.kubernetes.io/managed-by: patchpilot-installer
spec:
  capacity:
    storage: ${size}
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: ${reclaim}
  storageClassName: ${sc}
  claimRef:
    apiVersion: v1
    kind: PersistentVolumeClaim
    name: ${name}
    namespace: ${PP_NAMESPACE}
${src}
PVDOC
  }

  # _volname: "  volumeName: <name>" for static PVs (binds the PVC to its named
  # PV), or empty for dynamic mode (the provisioner picks the PV).
  _volname() { [[ "$2" == "dynamic" ]] && printf '' || printf '  volumeName: %s' "$1"; }

  # Local volumes (postgres, ansible, and local backups) share one mode.
  local _local_prov_mode; _local_prov_mode="$(_local_mode "${PP_POSTGRES_STORAGE_CLASS}")"
  PP_POSTGRES_MODE="${_local_prov_mode}"
  PP_ANSIBLE_MODE="${_local_prov_mode}"
  if [[ "${PP_BACKUP_STORAGE_TYPE}" == "nfs" ]]; then
    PP_BACKUPS_MODE="nfs"
  else
    PP_BACKUPS_MODE="${_local_prov_mode}"
  fi

  if [[ "${_local_prov_mode}" == "dynamic" ]]; then
    info "Local volumes use dynamic provisioning (StorageClass: ${PP_POSTGRES_STORAGE_CLASS}) — no hostPath on the node"
  else
    info "Local volumes use static hostPath under ${PP_DATA_DIR}"
  fi

  PP_POSTGRES_PV_DOC="$(_pv_doc patchpilot-postgres-data postgres "${PP_POSTGRES_STORAGE_SIZE}" Delete "${PP_POSTGRES_STORAGE_CLASS}" "${PP_POSTGRES_MODE}")"
  PP_BACKUPS_PV_DOC="$(_pv_doc patchpilot-backups       backups  "${PP_BACKUPS_STORAGE_SIZE}" Retain "${PP_APP_STORAGE_CLASS}"      "${PP_BACKUPS_MODE}")"
  PP_ANSIBLE_PV_DOC="$(_pv_doc patchpilot-ansible-data  ansible  "${PP_ANSIBLE_STORAGE_SIZE}" Delete "${PP_ANSIBLE_STORAGE_CLASS}"  "${PP_ANSIBLE_MODE}")"

  PP_POSTGRES_VOLNAME="$(_volname patchpilot-postgres-data "${PP_POSTGRES_MODE}")"
  PP_BACKUPS_VOLNAME="$(_volname patchpilot-backups "${PP_BACKUPS_MODE}")"
  PP_ANSIBLE_VOLNAME="$(_volname patchpilot-ansible-data "${PP_ANSIBLE_MODE}")"

  # ── kubectl path — baked into the deployment so the backend pod can run
  #    kubectl without relying on PATH inside the slim Python image.
  #    We resolve it NOW on the install host where kubectl is guaranteed present.
  PP_KUBECTL_BIN="$(which kubectl)"
  ok "kubectl resolved: ${PP_KUBECTL_BIN}"

  # ── Summary ────────────────────────────────────────────────────────────────
  echo ""
  echo -e "${CYAN}Configuration summary:${NC}"
  echo "  Namespace      : ${PP_NAMESPACE}"
  echo "  Version        : ${PP_VERSION}"
  echo "  Cluster arch   : ${PP_TARGET_ARCH}"
  echo "  Backend image  : ${PP_BACKEND_IMAGE}"
  echo "  Frontend image : ${PP_FRONTEND_IMAGE}"
  echo "  Primary host   : ${PP_HOSTNAME}"
  echo "  TLS enabled    : ${PP_TLS_ENABLED}$([[ "${PP_TLS_ENABLED}" == "true" ]] && echo " (${PP_TLS_MODE})")"
  echo "  Backup storage : ${PP_BACKUP_STORAGE_TYPE}${PP_NFS_SERVER:+ (${PP_NFS_SERVER}:${PP_NFS_SHARE})}"
  echo "  Data directory : ${PP_DATA_DIR}"
  echo ""
}

# ── Build images ───────────────────────────────────────────────────────────────
build_images() {
  step "Building Docker images"

  # When strategy=registry the images are already published on DockerHub.
  # Skip the local build entirely — no Docker daemon required.
  if [[ "${PP_IMAGE_STRATEGY}" == "registry" ]]; then
    ok "strategy=registry — using pre-built images from DockerHub, skipping local build"
    export PP_BUILDX_PUSH=true   # signal push step to skip docker push too
    return 0
  fi

  local target_platform host_platform
  target_platform="linux/${PP_TARGET_ARCH//-//}"   # arm-v7 → arm/v7
  host_platform="linux/$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"

  info "Build host platform    : ${host_platform}"
  info "Cluster target platform: ${target_platform}"

  if [[ "${target_platform}" == "${host_platform}" ]]; then
    info "Same-arch build — using docker build"
    docker build --file "${REPO_ROOT}/Dockerfile" \
      --platform "${target_platform}" --tag "${PP_BACKEND_IMAGE}" "${REPO_ROOT}"
    ok "Backend built: ${PP_BACKEND_IMAGE}"
    docker build --file "${REPO_ROOT}/Dockerfile.frontend" \
      --platform "${target_platform}" --tag "${PP_FRONTEND_IMAGE}" "${REPO_ROOT}"
    ok "Frontend built: ${PP_FRONTEND_IMAGE}"
    PP_BUILDX_PUSH=false
  else
    info "Cross-arch build (${host_platform} → ${target_platform}) — using buildx + push"
    if ! docker buildx inspect patchpilot-builder &>/dev/null; then
      docker buildx create --name patchpilot-builder --platform linux/amd64,linux/arm64 --use
    else
      docker buildx use patchpilot-builder
    fi
    echo "${PP_DH_TOKEN}" | docker login --username "${PP_DH_USERNAME}" --password-stdin
    ok "Logged in to Docker Hub"
    docker buildx build --no-cache --platform "${target_platform}" \
      --file "${REPO_ROOT}/Dockerfile" --tag "${PP_BACKEND_IMAGE}" --push "${REPO_ROOT}"
    ok "Backend built + pushed: ${PP_BACKEND_IMAGE}"
    docker buildx build --no-cache --platform "${target_platform}" \
      --file "${REPO_ROOT}/Dockerfile.frontend" --tag "${PP_FRONTEND_IMAGE}" --push "${REPO_ROOT}"
    ok "Frontend built + pushed: ${PP_FRONTEND_IMAGE}"
    docker logout &>/dev/null || true
    PP_BUILDX_PUSH=true
  fi
  export PP_BUILDX_PUSH PP_TARGET_PLATFORM="${target_platform}"
}

# ── Push + create pull secret ──────────────────────────────────────────────────
push_and_configure_registry() {
  step "Pushing images to Docker Hub"

  if [[ "${PP_BUILDX_PUSH:-false}" == "true" ]]; then
    ok "strategy=registry — images pulled from DockerHub at deploy time, no local push"
  else
    echo "${PP_DH_TOKEN}" | docker login --username "${PP_DH_USERNAME}" --password-stdin
    ok "Logged in"
    for img in "${PP_BACKEND_IMAGE}" "${PP_FRONTEND_IMAGE}"; do
      info "Pushing ${img}..."
      docker push "${img}"
      ok "Pushed: ${img}"
    done
    docker logout &>/dev/null || true
  fi

  step "Creating imagePullSecret in cluster"
  kubectl create namespace "${PP_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - &>/dev/null

  if [[ -n "${PP_DH_USERNAME}" && -n "${PP_DH_TOKEN}" ]]; then
    kubectl create secret docker-registry "${PP_PULL_SECRET_NAME}" \
      --namespace="${PP_NAMESPACE}" \
      --docker-server="https://index.docker.io/v1/" \
      --docker-username="${PP_DH_USERNAME}" \
      --docker-password="${PP_DH_TOKEN}" \
      --dry-run=client -o yaml | kubectl apply -f -
    ok "imagePullSecret '${PP_PULL_SECRET_NAME}' ready"
  else
    # Public image — no pull secret needed. Create a placeholder so manifest
    # templates that reference imagePullSecrets don't error on a missing secret.
    kubectl create secret generic "${PP_PULL_SECRET_NAME}" \
      --namespace="${PP_NAMESPACE}" \
      --from-literal=placeholder=true \
      --dry-run=client -o yaml | kubectl apply -f - &>/dev/null
    ok "Public image — no pull secret needed (placeholder created)"
  fi
}

# ── Generate manifests ─────────────────────────────────────────────────────────
generate_manifests() {
  step "Generating Kubernetes manifests"
  mkdir -p "${GENERATED_DIR}"
  rm -f "${GENERATED_DIR}"/*.yaml
  local tmpl="${SCRIPT_DIR}/templates"

  render() {
    export PP_NAMESPACE PP_VERSION PP_BACKEND_IMAGE PP_FRONTEND_IMAGE \
           PP_IMAGE_PULL_POLICY PP_PULL_SECRET_NAME \
           PP_DB_USER PP_DB_PASSWORD PP_DB_NAME \
           PP_ENCRYPTION_KEY PP_BASE_URL PP_ALLOWED_ORIGINS \
           PP_AUTO_REFRESH_INTERVAL PP_DEFAULT_SSH_USER PP_DEFAULT_SSH_PORT \
           PP_BACKUP_RETAIN_COUNT PP_MAX_BACKUP_SIZE_MB \
           PP_POSTGRES_STORAGE_SIZE PP_BACKUPS_STORAGE_SIZE PP_ANSIBLE_STORAGE_SIZE \
           PP_POSTGRES_STORAGE_CLASS_SPEC PP_APP_STORAGE_CLASS_SPEC \
           PP_POSTGRES_PV_DOC PP_BACKUPS_PV_DOC PP_ANSIBLE_PV_DOC \
           PP_POSTGRES_VOLNAME PP_BACKUPS_VOLNAME PP_ANSIBLE_VOLNAME \
           PP_POSTGRES_STORAGE_CLASS PP_APP_STORAGE_CLASS \
           PP_ANSIBLE_STORAGE_CLASS PP_ANSIBLE_STORAGE_CLASS_SPEC \
           PP_NFS_SERVER PP_NFS_SHARE PP_BACKUP_STORAGE_TYPE \
           PP_CLUSTER_ISSUER PP_TLS_SECRET_NAME PP_INGRESS_CLASS \
           PP_CERT_MANAGER_ANNOTATION \
           PP_LE_EMAIL PP_CF_EMAIL PP_CF_API_TOKEN_SECRET \
           PP_KUBECTL_BIN PP_DATA_DIR \
           PP_LICENSE_PROVIDER PP_FREEMIUS_PRODUCT_ID
    : "${PP_LICENSE_PROVIDER:=freemius}"
    : "${PP_FREEMIUS_PRODUCT_ID:=28811}"
    envsubst '$PP_NAMESPACE:$PP_VERSION:$PP_BACKEND_IMAGE:$PP_FRONTEND_IMAGE:'\
'$PP_IMAGE_PULL_POLICY:$PP_PULL_SECRET_NAME:$PP_DB_USER:$PP_DB_PASSWORD:'\
'$PP_DB_NAME:$PP_ENCRYPTION_KEY:$PP_BASE_URL:$PP_ALLOWED_ORIGINS:'\
'$PP_POSTGRES_STORAGE_SIZE:$PP_BACKUPS_STORAGE_SIZE:$PP_ANSIBLE_STORAGE_SIZE:'\
'$PP_POSTGRES_PV_DOC:$PP_BACKUPS_PV_DOC:$PP_ANSIBLE_PV_DOC:'\
'$PP_POSTGRES_VOLNAME:$PP_BACKUPS_VOLNAME:$PP_ANSIBLE_VOLNAME:'\
'$PP_POSTGRES_STORAGE_CLASS:$PP_POSTGRES_STORAGE_CLASS_SPEC:'\
'$PP_APP_STORAGE_CLASS:$PP_APP_STORAGE_CLASS_SPEC:'\
'$PP_ANSIBLE_STORAGE_CLASS:$PP_ANSIBLE_STORAGE_CLASS_SPEC:'\
'$PP_NFS_SERVER:$PP_NFS_SHARE:$PP_BACKUP_STORAGE_TYPE:'\
'$PP_CLUSTER_ISSUER:$PP_TLS_SECRET_NAME:$PP_INGRESS_CLASS:'\
'$PP_LE_EMAIL:$PP_CF_EMAIL:$PP_CF_API_TOKEN_SECRET:'\
'$PP_AUTO_REFRESH_INTERVAL:$PP_DEFAULT_SSH_USER:$PP_DEFAULT_SSH_PORT:'\
'$PP_BACKUP_RETAIN_COUNT:$PP_MAX_BACKUP_SIZE_MB:'\
'$PP_INGRESS_RULES:$PP_TLS_HOSTS:$PP_TLS_DNS_NAMES:'\
'$PP_INGRESS_MIDDLEWARE_ANNOTATION:$PP_CERT_MANAGER_ANNOTATION:$PP_HOSTNAME:$PP_KUBECTL_BIN:$PP_DATA_DIR:'\
'$PP_LICENSE_PROVIDER:$PP_FREEMIUS_PRODUCT_ID' \
      < "$1" > "$2"
  }

  render "${tmpl}/00-namespace.yaml"  "${GENERATED_DIR}/00-namespace.yaml";  ok "00-namespace.yaml"
  render "${tmpl}/00b-rbac.yaml"      "${GENERATED_DIR}/00b-rbac.yaml";      ok "00b-rbac.yaml"
  render "${tmpl}/01-secrets.yaml"    "${GENERATED_DIR}/01-secrets.yaml";    ok "01-secrets.yaml"
  render "${tmpl}/02-pvs.yaml"        "${GENERATED_DIR}/02-pvs.yaml"
  # All volumes dynamic → no static PVs → 02-pvs.yaml is comment-only, which
  # `kubectl apply` rejects ("no objects passed to apply"). Drop the file so the
  # apply loop skips it.
  if ! grep -q 'kind: PersistentVolume' "${GENERATED_DIR}/02-pvs.yaml"; then
    rm -f "${GENERATED_DIR}/02-pvs.yaml"
    ok "02-pvs.yaml (none needed — all volumes dynamically provisioned)"
  else
    ok "02-pvs.yaml"
  fi
  render "${tmpl}/02b-pvcs.yaml"      "${GENERATED_DIR}/02b-pvcs.yaml";      ok "02b-pvcs.yaml"
  render "${tmpl}/03-postgres.yaml"   "${GENERATED_DIR}/03-postgres.yaml";   ok "03-postgres.yaml"
  render "${tmpl}/04-backend.yaml"    "${GENERATED_DIR}/04-backend.yaml";    ok "04-backend.yaml"
  render "${tmpl}/05-frontend.yaml"   "${GENERATED_DIR}/05-frontend.yaml";   ok "05-frontend.yaml"

  if [[ "${PP_TLS_ENABLED}" == "true" ]]; then
    render "${tmpl}/06-middlewares-https.yaml" "${GENERATED_DIR}/06-middlewares.yaml"
    ok "06-middlewares.yaml (HTTPS)"
    # cert-manager Certificate only in ACME mode. In byo mode the TLS secret is
    # pre-created by the operator, so no Certificate (and no cert-manager) at all.
    if [[ "${PP_TLS_MODE}" == "acme" ]]; then
      local dns_names=""
      for h in "${ALL_HOSTNAMES[@]}"; do dns_names+="    - ${h}"$'\n'; done
      export PP_TLS_DNS_NAMES="${dns_names%$'\n'}"
      render "${tmpl}/07-certificate.yaml" "${GENERATED_DIR}/07-certificate.yaml"
      ok "07-certificate.yaml"
    else
      ok "07-certificate.yaml (skipped — bring-your-own cert)"
    fi
  fi

  local ingress_rules=""
  for h in "${ALL_HOSTNAMES[@]}"; do
    ingress_rules+="  - host: ${h}"$'\n'
    ingress_rules+="    http:"$'\n'
    ingress_rules+="      paths:"$'\n'
    ingress_rules+="      - path: /"$'\n'
    ingress_rules+="        pathType: Prefix"$'\n'
    ingress_rules+="        backend:"$'\n'
    ingress_rules+="          service:"$'\n'
    ingress_rules+="            name: patchpilot-frontend"$'\n'
    ingress_rules+="            port:"$'\n'
    ingress_rules+="              number: 80"$'\n'
  done
  export PP_INGRESS_RULES="${ingress_rules%$'\n'}"

  if [[ "${PP_TLS_ENABLED}" == "true" ]]; then
    local tls_hosts=""
    for h in "${ALL_HOSTNAMES[@]}"; do tls_hosts+="    - ${h}"$'\n'; done
    export PP_TLS_HOSTS="${tls_hosts%$'\n'}"

    local mw=""
    [[ "${PP_HTTPS_REDIRECT}" == "true" ]]   && mw="${PP_NAMESPACE}-patchpilot-https-redirect@kubernetescrd"
    [[ "${PP_SECURITY_HEADERS}" == "true" ]] && mw="${mw:+${mw},}${PP_NAMESPACE}-patchpilot-security-headers@kubernetescrd"
    export PP_INGRESS_MIDDLEWARE_ANNOTATION=""
    [[ -n "${mw}" ]] && PP_INGRESS_MIDDLEWARE_ANNOTATION="    traefik.ingress.kubernetes.io/router.middlewares: \"${mw}\""

    # cert-manager annotation only in ACME mode; byo mode references the secret
    # directly with no cert-manager involvement.
    export PP_CERT_MANAGER_ANNOTATION=""
    [[ "${PP_TLS_MODE}" == "acme" ]] && \
      PP_CERT_MANAGER_ANNOTATION="    cert-manager.io/cluster-issuer: \"${PP_CLUSTER_ISSUER}\""

    render "${tmpl}/08-ingress-https.yaml" "${GENERATED_DIR}/08-ingress.yaml"
    ok "08-ingress.yaml (HTTPS)"
  else
    render "${tmpl}/08-ingress-http.yaml" "${GENERATED_DIR}/08-ingress.yaml"
    ok "08-ingress.yaml (HTTP)"
  fi

  if [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "acme" && "${PP_CREATE_CLUSTER_ISSUER}" == "true" ]]; then
    case "${PP_CHALLENGE_TYPE}" in
      dns01-cloudflare)
        render "${tmpl}/09-clusterissuer-cloudflare.yaml" "${GENERATED_DIR}/09-clusterissuer.yaml"
        ok "09-clusterissuer.yaml (Cloudflare DNS-01)" ;;
      http01)
        render "${tmpl}/09-clusterissuer-http01.yaml" "${GENERATED_DIR}/09-clusterissuer.yaml"
        ok "09-clusterissuer.yaml (HTTP-01)" ;;
      *) warn "Unknown challengeType '${PP_CHALLENGE_TYPE}' — skipping ClusterIssuer" ;;
    esac
  fi

  ok "All manifests in: ${GENERATED_DIR}/"
}

# ── Validate StorageClasses ────────────────────────────────────────────────────
validate_storage_classes() {
  step "Validating StorageClasses"
  local available_scs
  available_scs="$(kubectl get sc -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"

  # volume_label:storageclass — each volume the installer provisions, so a
  # failure names the specific volume (postgres/backups/ansible) at fault.
  local checks=(
    "postgres:${PP_POSTGRES_STORAGE_CLASS}"
    "backups:${PP_APP_STORAGE_CLASS}"
    "ansible:${PP_ANSIBLE_STORAGE_CLASS}"
  )
  local failures=()
  local seen_ok=" "
  local entry vol sc_var
  for entry in "${checks[@]}"; do
    vol="${entry%%:*}"
    sc_var="${entry#*:}"
    [[ -z "${sc_var}" ]] && continue
    if echo "${available_scs}" | tr ' ' '\n' | grep -qx "${sc_var}"; then
      # Only report/warn once per distinct StorageClass to cut noise.
      if [[ "${seen_ok}" != *" ${sc_var} "* ]]; then
        ok "StorageClass exists: ${sc_var}"
        local binding_mode
        binding_mode="$(kubectl get sc "${sc_var}" -o jsonpath='{.volumeBindingMode}' 2>/dev/null)"
        if [[ "${binding_mode}" == "WaitForFirstConsumer" ]]; then
          warn "StorageClass '${sc_var}' uses WaitForFirstConsumer — PVCs will pend until pod schedules"
          PP_SC_WAIT_FOR_CONSUMER="true"
        fi
        seen_ok+="${sc_var} "
      fi
    else
      err "StorageClass '${sc_var}' (${vol} volume) not found in cluster"
      failures+=("${vol}:${sc_var}")
    fi
  done

  if [[ ${#failures[@]} -gt 0 ]]; then
    echo "" >&2
    err "Available StorageClasses in cluster:"
    kubectl get sc --no-headers 2>/dev/null | awk '{print "    " $1 "  (" $2 ")"}' >&2
    die "StorageClass validation failed for: ${failures[*]} — fix config and retry."
  fi
}

# ── Validate bring-your-own TLS secret ──────────────────────────────────────────
validate_tls_byo() {
  [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "byo" ]] || return 0
  step "Validating bring-your-own TLS secret"
  if kubectl get secret "${PP_TLS_SECRET_NAME}" -n "${PP_NAMESPACE}" &>/dev/null; then
    local stype
    stype="$(kubectl get secret "${PP_TLS_SECRET_NAME}" -n "${PP_NAMESPACE}" \
      -o jsonpath='{.type}' 2>/dev/null || true)"
    if [[ "${stype}" == "kubernetes.io/tls" ]]; then
      ok "TLS secret '${PP_TLS_SECRET_NAME}' found in namespace ${PP_NAMESPACE}"
    else
      warn "Secret '${PP_TLS_SECRET_NAME}' is type '${stype}' (expected kubernetes.io/tls) — Traefik may not serve your cert"
    fi
  else
    warn "TLS secret '${PP_TLS_SECRET_NAME}' not found in namespace ${PP_NAMESPACE}."
    warn "Create it from your wildcard cert (Traefik serves a default self-signed cert until you do):"
    warn "  kubectl create secret tls ${PP_TLS_SECRET_NAME} --cert=your.crt --key=your.key -n ${PP_NAMESPACE}"
  fi
  return 0
}

# ── Wait for PVCs ──────────────────────────────────────────────────────────────
wait_for_pvcs() {
  local pvcs=("patchpilot-postgres-data" "patchpilot-backups" "patchpilot-ansible-data")
  if [[ "${PP_SC_WAIT_FOR_CONSUMER:-false}" == "true" ]]; then
    info "WaitForFirstConsumer mode — skipping PVC pre-bind wait"
    return 0
  fi
  info "Waiting for PVCs to bind (up to 90s)..."
  local deadline=$(( $(date +%s) + 90 ))
  local tmpdir; tmpdir="$(mktemp -d)"
  for pvc in "${pvcs[@]}"; do echo "Pending" > "${tmpdir}/${pvc}"; done
  while [[ $(date +%s) -lt ${deadline} ]]; do
    local all_bound=true
    for pvc in "${pvcs[@]}"; do
      [[ "$(cat "${tmpdir}/${pvc}")" == "Bound" ]] && continue
      local phase
      phase="$(kubectl get pvc "${pvc}" -n "${PP_NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null)"
      echo "${phase:-Unknown}" > "${tmpdir}/${pvc}"
      [[ "${phase}" == "Bound" ]] && ok "${pvc}: Bound" || all_bound=false
    done
    [[ "${all_bound}" == "true" ]] && break
    sleep 5
  done
  local failed=false
  for pvc in "${pvcs[@]}"; do
    local s; s="$(cat "${tmpdir}/${pvc}")"
    if [[ "${s}" != "Bound" ]]; then
      err "${pvc}: ${s}"; failed=true
    fi
  done
  rm -rf "${tmpdir}"
  [[ "${failed}" == "true" ]] && die "PVC binding failed — check kubectl describe pvc -n ${PP_NAMESPACE}"
  return 0  # explicit: [[ ]] above returns 1 when false, which would kill set -e
}

# ── Clean node data dirs ───────────────────────────────────────────────────────
# FIX: use -t to allocate a TTY so sudo doesn't fail with
#      "sudo: a terminal is required to read the password"
clean_node_data_dirs() {
  # Dynamic provisioning keeps no data under ${PP_DATA_DIR} on the node — the
  # provisioner owns the volume directory and reclaims it on PVC deletion. There
  # is nothing to SSH in and remove, so skip this step entirely.
  if [[ "${PP_POSTGRES_MODE:-}" == "dynamic" ]]; then
    ok "Local volumes are dynamically provisioned — no hostPath data on the node to clean"
    return 0
  fi
  local node node_ip
  local data_dir; data_dir="$(yaml_get patchpilot.storage.dataDir /app-data)"
  data_dir="${data_dir%/}"
  node="$(kubectl get nodes --field-selector='spec.unschedulable!=true' \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
  node_ip="$(_pick_ip "$(kubectl get node "${node}" \
    -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)")"

  [[ -z "${node}" ]] && return 0

  step "Node data cleanup — ${node} (${node_ip})"

  # Exclude patchpilot-backups — it is intentionally retained for post-uninstall restore
  local cleanup_cmd="ssh ${node_ip} sudo rm -rf ${data_dir}/patchpilot-postgres-data ${data_dir}/patchpilot-ansible-data"

  # ── Check whether stale dirs actually exist before doing anything ──────────
  local dirs_exist="unknown"
  local ssh_target="${node_ip}"

  # Try by IP first, then by hostname
  for _target in "${node_ip}" "${node}"; do
    if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes         "${_target}" "test -d ${data_dir}" 2>/dev/null; then
      ssh_target="${_target}"
      if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
          "${_target}" "find ${data_dir} -maxdepth 1 -name 'patchpilot-*' ! -name 'patchpilot-backups' -print -quit 2>/dev/null | grep -q ." 2>/dev/null; then
        dirs_exist="yes"
      else
        dirs_exist="no"
      fi
      break
    fi
  done

  # ── Nothing to clean — skip entirely ──────────────────────────────────────
  if [[ "${dirs_exist}" == "no" ]]; then
    ok "No leftover patchpilot data on node — skipping cleanup"
    return 0
  fi

  # ── Dirs exist or SSH unreachable — pause for manual cleanup ──────────────
  if [[ "${dirs_exist}" == "unknown" ]]; then
    warn "Could not SSH to node ${node_ip} to check for stale data."
    warn "If this is a fresh install (not a reinstall) you can safely continue."
  else
    warn "Stale ${data_dir}/patchpilot-* found on node — must be removed before postgres can init."
  fi

  # Update cleanup_cmd to use the reachable target if we found one
  [[ "${dirs_exist}" == "yes" ]] && cleanup_cmd="ssh ${ssh_target} sudo rm -rf ${data_dir}/patchpilot-postgres-data ${data_dir}/patchpilot-ansible-data"

  if [[ "${NO_PROMPTS}" == "true" ]]; then
    # Web wizard path — emit structured pause marker and block on resume file
    echo "__PAUSE_CLEANUP__ ${cleanup_cmd}"
    info "Waiting for you to confirm cleanup is done in the browser..."

    local resume_file="/tmp/patchpilot-install-resume"
    rm -f "${resume_file}"
    local waited=0
    while [[ ! -f "${resume_file}" ]]; do
      sleep 1
      waited=$(( waited + 1 ))
      if [[ ${waited} -gt 600 ]]; then
        die "Timed out waiting for cleanup confirmation (10 min). Re-run the installer when ready."
      fi
    done
    rm -f "${resume_file}"
    ok "Cleanup confirmed — continuing install"
  else
    # Interactive CLI path — print command and wait for Enter
    warn "Run this on the node:"
    warn "  ${cleanup_cmd}"
    echo ""
    echo -en "${CYAN}Done? Press Enter to continue (Ctrl+C to abort): ${NC}"
    read -r _ignored
    ok "Continuing install"
  fi
}

# ── Apply manifests ────────────────────────────────────────────────────────────
apply_manifests() {
  step "Applying manifests to cluster"

  # ── Print generated manifests to log so we can see exactly what kubectl gets
  step "Generated manifests (pre-apply)"
  for manifest in "${GENERATED_DIR}"/*.yaml; do
    local base; base="$(basename "${manifest}")"
    info "────────── ${base} ──────────"
    cat "${manifest}"
    echo ""
  done

  # ── Re-adopt Released backups PV from a prior install ────────────────────
  # After uninstall the backups PV is left Released (Retain policy).
  # A Released PV won't bind to a new PVC until claimRef is cleared —
  # patching it to Available lets the new install reclaim it with data intact.
  local backups_pv="${PP_NAMESPACE}-backups"
  local pv_phase
  pv_phase="$(kubectl get pv "${backups_pv}" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [[ "${pv_phase}" == "Released" ]]; then
    info "Found existing ${backups_pv} PV in Released state — clearing claimRef so new install can bind to it..."
    kubectl patch pv "${backups_pv}" --type=json \
      -p='[{"op":"remove","path":"/spec/claimRef"}]' \
      && ok "${backups_pv} PV is now Available (existing backup data preserved)" \
      || warn "Could not patch ${backups_pv} PV — PVC may stay Pending"
  elif [[ -n "${pv_phase}" ]]; then
    info "${backups_pv} PV exists in phase '${pv_phase}' — no action needed"
  fi

  # ── Pre-check: reconcile PVs whose volume source changed ─────────────────
  # PV spec.persistentVolumeSource is immutable after creation.  If the NFS
  # server/path (or hostPath) changed since the last install, kubectl apply
  # will fail.
  #
  # Strategy per PV:
  #   postgres-data / ansible-data (reclaimPolicy: Delete) — disposable,
  #       safe to delete and recreate when the source changes.
  #   backups (reclaimPolicy: Retain) — NEVER delete.  This PV intentionally
  #       survives uninstall so backup archives are available for reinstall.
  #       If it already exists, re-adopt it as-is and strip it from the
  #       generated manifest so kubectl apply doesn't try to mutate it.

  _reconcile_pv() {
    local pv_name="$1"
    if ! kubectl get pv "${pv_name}" &>/dev/null; then
      return 0   # doesn't exist yet — nothing to do
    fi

    # Switched to dynamic provisioning: this volume no longer has a static PV in
    # the generated manifest (file dropped, or PV doc absent). The old static PV
    # (reclaimPolicy: Delete) would otherwise re-bind the new PVC and prevent the
    # provisioner from creating a fresh volume — delete it. Only postgres/ansible
    # reach here; the backups PV is never reconciled.
    if [[ ! -f "${GENERATED_DIR}/02-pvs.yaml" ]] || \
       ! grep -q "name: ${pv_name}\$" "${GENERATED_DIR}/02-pvs.yaml"; then
      info "${pv_name}: now dynamically provisioned — removing stale static PV"
      kubectl delete pv "${pv_name}" --wait=false 2>/dev/null || true
      for _ in $(seq 1 10); do
        kubectl get pv "${pv_name}" &>/dev/null || break
        sleep 1
      done
      ok "PV ${pv_name} deleted — provisioner will create the volume"
      return 0
    fi

    # Extract the volume source from the *generated* manifest
    local new_nfs_server new_nfs_path new_host_path
    new_nfs_server="$(awk "/name: ${pv_name}\$/,/^---/{print}" "${GENERATED_DIR}/02-pvs.yaml" \
      | grep -A1 '^ *nfs:' | awk '/server:/{print $2}')"
    new_nfs_path="$(awk "/name: ${pv_name}\$/,/^---/{print}" "${GENERATED_DIR}/02-pvs.yaml" \
      | grep -A2 '^ *nfs:' | awk '/path:/{print $2}')"
    new_host_path="$(awk "/name: ${pv_name}\$/,/^---/{print}" "${GENERATED_DIR}/02-pvs.yaml" \
      | grep -A1 '^ *hostPath:' | awk '/path:/{print $2}')"

    # Extract current source from cluster
    local cur_nfs_server cur_nfs_path cur_host_path
    cur_nfs_server="$(kubectl get pv "${pv_name}" -o jsonpath='{.spec.nfs.server}' 2>/dev/null || true)"
    cur_nfs_path="$(kubectl get pv "${pv_name}" -o jsonpath='{.spec.nfs.path}' 2>/dev/null || true)"
    cur_host_path="$(kubectl get pv "${pv_name}" -o jsonpath='{.spec.hostPath.path}' 2>/dev/null || true)"

    local changed=false
    if [[ -n "${new_nfs_server}" && -n "${cur_host_path}" && -z "${cur_nfs_server}" ]]; then
      changed=true; info "${pv_name}: source changed from hostPath to NFS"
    elif [[ -n "${new_host_path}" && -n "${cur_nfs_server}" && -z "${cur_host_path}" ]]; then
      changed=true; info "${pv_name}: source changed from NFS to hostPath"
    elif [[ -n "${new_nfs_server}" && "${new_nfs_server}" != "${cur_nfs_server}" ]]; then
      changed=true; info "${pv_name}: NFS server changed (${cur_nfs_server} → ${new_nfs_server})"
    elif [[ -n "${new_nfs_path}" && "${new_nfs_path}" != "${cur_nfs_path}" ]]; then
      changed=true; info "${pv_name}: NFS path changed (${cur_nfs_path} → ${new_nfs_path})"
    elif [[ -n "${new_host_path}" && "${new_host_path}" != "${cur_host_path}" ]]; then
      changed=true; info "${pv_name}: hostPath changed (${cur_host_path} → ${new_host_path})"
    fi

    if [[ "${changed}" == "true" ]]; then
      info "Deleting stale PV ${pv_name} so it can be recreated with the new source..."
      kubectl delete pv "${pv_name}" --wait=false 2>/dev/null || true
      for _ in $(seq 1 10); do
        kubectl get pv "${pv_name}" &>/dev/null || break
        sleep 1
      done
      ok "PV ${pv_name} deleted — will be recreated on apply"
    fi
  }

  # Only reconcile disposable PVs — NEVER touch patchpilot-backups
  _reconcile_pv "patchpilot-postgres-data"
  _reconcile_pv "patchpilot-ansible-data"

  # ── Protect existing backups PV ─────────────────────────────────────────
  # If patchpilot-backups PV already exists (Retain from prior install),
  # strip it from the generated manifest so kubectl apply doesn't try to
  # mutate the immutable volume source.  The re-adoption (claimRef clear)
  # was already handled above.
  if kubectl get pv "patchpilot-backups" &>/dev/null \
     && [[ -f "${GENERATED_DIR}/02-pvs.yaml" ]] \
     && grep -q "name: patchpilot-backups\$" "${GENERATED_DIR}/02-pvs.yaml"; then
    info "patchpilot-backups PV already exists — stripping from manifest to preserve it"
    # Use awk to remove the backups PV document from the multi-doc YAML.
    # Each document starts with "---" and the backups PV contains "name: patchpilot-backups".
    awk '
      BEGIN { skip=0; buf="" }
      /^---/ {
        if (!skip && buf != "") printf "%s", buf
        skip=0; buf=$0"\n"; next
      }
      { buf = buf $0 "\n" }
      /name: patchpilot-backups/ { skip=1 }
      END { if (!skip && buf != "") printf "%s", buf }
    ' "${GENERATED_DIR}/02-pvs.yaml" > "${GENERATED_DIR}/02-pvs.yaml.tmp" \
      && mv "${GENERATED_DIR}/02-pvs.yaml.tmp" "${GENERATED_DIR}/02-pvs.yaml"
    ok "patchpilot-backups PV preserved (not re-applied)"
    # If backups was the only static PV (postgres/ansible are dynamic), the file
    # is now comment-only — kubectl apply rejects that ("no objects passed to
    # apply"). Drop it so the apply loop skips it.
    if ! grep -q 'kind: PersistentVolume' "${GENERATED_DIR}/02-pvs.yaml"; then
      rm -f "${GENERATED_DIR}/02-pvs.yaml"
      ok "02-pvs.yaml now empty — nothing static to apply, skipping"
    fi
  fi

  # ── Apply each manifest, showing full kubectl output ─────────────────────
  step "Applying manifests"
  for manifest in "${GENERATED_DIR}"/*.yaml; do
    local base; base="$(basename "${manifest}")"
    # Skip comment-only / empty manifests — kubectl apply errors with
    # "no objects passed to apply" on them.
    if ! grep -q '^[[:space:]]*kind:' "${manifest}"; then
      info "Skipping ${base} (no objects)"
      continue
    fi
    info "kubectl apply -f ${base}..."
    kubectl apply -f "${manifest}"   # full output, no suppression
    info "  → applied OK"
    if [[ "${base}" == 02b-* ]]; then wait_for_pvcs; fi
  done

  ok "All manifests applied"

  # ── Dump full cluster state ───────────────────────────────────────────────
  step "Cluster state after apply"
  info "All objects in ${PP_NAMESPACE}:"
  kubectl get all -n "${PP_NAMESPACE}" 2>&1 || true
  echo ""
  info "Namespace events:"
  kubectl get events -n "${PP_NAMESPACE}" --sort-by=.lastTimestamp 2>&1 | tail -25 || true
  echo ""
}

# ── Dump diagnostics for a failed/stuck deployment ────────────────────────────
dump_pod_diagnostics() {
  local deploy="$1"
  echo ""
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  err "Diagnostics for ${deploy}"
  err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  echo ""
  info "Pod status:"
  kubectl get pods -n "${PP_NAMESPACE}" -l "app=${deploy}" 2>/dev/null || true

  echo ""
  info "Pod describe (latest):"
  local pod
  pod="$(kubectl get pods -n "${PP_NAMESPACE}" -l "app=${deploy}"     --sort-by=.metadata.creationTimestamp     -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null)"
  if [[ -n "${pod}" ]]; then
    kubectl describe pod "${pod}" -n "${PP_NAMESPACE}" 2>/dev/null | tail -40
    echo ""
    info "Last 30 log lines:"
    kubectl logs "${pod}" -n "${PP_NAMESPACE}" --tail=30 2>/dev/null ||       info "(no logs yet — container may not have started)"
  else
    warn "No pods found for ${deploy} — deployment may not have been created"
    info "All objects in namespace:"
    kubectl get all -n "${PP_NAMESPACE}" 2>/dev/null || true
  fi

  echo ""
  info "Recent namespace events:"
  kubectl get events -n "${PP_NAMESPACE}"     --sort-by=.lastTimestamp 2>/dev/null | tail -20 || true
  echo ""
}

# ── Retain dynamically-provisioned backups ─────────────────────────────────────
# When backups use a dynamic provisioner (local disk on a local-path-style SC),
# the provisioned PV inherits the StorageClass reclaim policy (usually Delete),
# so backup archives would be wiped on uninstall. Patch the bound PV to Retain
# and tag it so the data survives — matching the guarantee the static NFS/backups
# PV gives. NFS backups are a static PV and already Retain, so they skip this.
# Note: auto re-adoption of a Retained dynamic backups PV on reinstall is not yet
# implemented — the old PV is left Released and a fresh volume is provisioned; the
# retained data can be recovered by manually rebinding that PV.
retain_dynamic_backups() {
  [[ "${PP_BACKUPS_MODE:-}" == "dynamic" ]] || return 0
  step "Securing dynamically-provisioned backups volume"
  local pv
  pv="$(kubectl get pvc patchpilot-backups -n "${PP_NAMESPACE}" \
    -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
  if [[ -z "${pv}" ]]; then
    warn "Backups PVC not bound yet — could not set Retain (backups may be deleted on uninstall)"
    return 0
  fi
  if kubectl patch pv "${pv}" \
       -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}' &>/dev/null \
     && kubectl annotate pv "${pv}" patchpilot.io/role=backups --overwrite &>/dev/null; then
    ok "Backups volume ${pv} set to Retain — survives uninstall"
  else
    warn "Could not set Retain on backups PV ${pv} — verify reclaim policy manually"
  fi
  return 0
}

# ── Wait for rollout ───────────────────────────────────────────────────────────
wait_for_rollout() {
  step "Waiting for deployments to become ready"
  local failed=false

  for deploy in patchpilot-postgres patchpilot-backend patchpilot-frontend; do
    info "Waiting for ${deploy} (timeout 180s)..."

    # Check deployment actually exists first
    if ! kubectl get deployment "${deploy}" -n "${PP_NAMESPACE}" &>/dev/null; then
      err "Deployment '${deploy}' not found in namespace '${PP_NAMESPACE}'"
      err "This usually means the manifest failed to apply or the image could not be pulled."
      dump_pod_diagnostics "${deploy}"
      failed=true
      continue
    fi

    if ! kubectl rollout status deployment/"${deploy}"         -n "${PP_NAMESPACE}" --timeout=180s; then
      err "Rollout timeout for ${deploy}"
      dump_pod_diagnostics "${deploy}"
      failed=true
    else
      ok "${deploy}: ready"
    fi
  done

  if [[ "${failed}" == "true" ]]; then
    echo ""
    err "One or more deployments failed to become ready."
    err "Review the diagnostics above."
    err "Common causes:"
    err "  • Image pull failure — check Docker Hub credentials and image tag"
    err "  • Wrong architecture tag — run: kubectl describe pod -n ${PP_NAMESPACE}"
    err "  • PVC not bound — run: kubectl get pvc -n ${PP_NAMESPACE}"
    err "  • Resource limits — check node capacity: kubectl describe nodes"
    echo ""
    err "To retry after fixing:"
    err "  kubectl delete namespace ${PP_NAMESPACE}"
    err "  ./install.sh --k3s   (or --web)"
    die "Installation failed — see diagnostics above."
  fi

  ok "All deployments ready"
}

# ── Completion ─────────────────────────────────────────────────────────────────
show_completion() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo -e "${GREEN}🎉  PatchPilot v${PP_VERSION} deployed to k3s!${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  [[ "${PP_TLS_ENABLED}" == "true" ]] \
    && echo -e "${BLUE}📊 Dashboard:${NC}  https://${PP_HOSTNAME}" \
    || echo -e "${BLUE}📊 Dashboard:${NC}  http://${PP_HOSTNAME}"
  echo -e "${BLUE}🔍 Namespace:${NC}  ${PP_NAMESPACE}"
  echo ""
  echo -e "${PURPLE}Commands:${NC}"
  echo "  kubectl get pods -n ${PP_NAMESPACE}"
  echo "  kubectl logs -n ${PP_NAMESPACE} -l app=patchpilot-backend -f"
  echo "  ./k8s/install-k3s.sh --uninstall"
  echo ""
  if [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "acme" ]]; then
    echo -e "${YELLOW}⏳ TLS:${NC} Certificate may take 1–3 min via Let's Encrypt."
  elif [[ "${PP_TLS_ENABLED}" == "true" && "${PP_TLS_MODE}" == "byo" ]]; then
    echo -e "${YELLOW}🔐 TLS:${NC} serving your own cert from secret '${PP_TLS_SECRET_NAME}'."
    echo -e "    If it isn't created yet: kubectl create secret tls ${PP_TLS_SECRET_NAME} --cert=your.crt --key=your.key -n ${PP_NAMESPACE}"
  fi
  echo ""

  # Emit structured credentials marker for the web installer's summary screen.
  # The web installer's SSE handler intercepts this line and displays the
  # credentials on the success banner so the user doesn't have to scroll.
  # Terminal users already saw the password near the top of output.
  echo "__CREDENTIALS__{\"pg_password\":\"${PP_DB_PASSWORD}\",\"encryption_key\":\"${PP_ENCRYPTION_KEY}\"}"
}

# ── Dry run ────────────────────────────────────────────────────────────────────
show_dry_run() {
  step "DRY RUN — manifests generated, NOT applied"
  ls -1 "${GENERATED_DIR}/"
  echo ""
  echo -e "${CYAN}Apply manually:${NC}  kubectl apply -f ${GENERATED_DIR}/"
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  print_banner
  [[ "${UNINSTALL}" == "true" ]] && do_uninstall
  check_prerequisites
  load_config

  if [[ "${DRY_RUN}" == "false" ]]; then
    confirm_proceed "Proceed with installation?" || { info "Aborted."; exit 0; }
    echo ""
  fi

  build_images
  push_and_configure_registry
  generate_manifests

  if [[ "${DRY_RUN}" == "true" ]]; then show_dry_run; exit 0; fi

  validate_storage_classes
  validate_tls_byo
  clean_node_data_dirs
  apply_manifests
  wait_for_rollout
  retain_dynamic_backups
  show_completion
}

main "$@"
