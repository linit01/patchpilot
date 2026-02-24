#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot v0.9.4-alpha — K3s Installer
#
# Usage:
#   ./k8s/install-k3s.sh                    # Uses k8s/install-config.yaml
#   ./k8s/install-k3s.sh --config my.yaml   # Custom config file
#   ./k8s/install-k3s.sh --interactive      # Force interactive prompts
#   ./k8s/install-k3s.sh --no-interactive   # Skip all prompts (web wizard mode)
#   ./k8s/install-k3s.sh --dry-run          # Generate manifests, don't apply
#   ./k8s/install-k3s.sh --uninstall        # Remove PatchPilot from cluster
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
DRY_RUN=false
INTERACTIVE=false
NO_PROMPTS=false
UNINSTALL=false
PP_SC_WAIT_FOR_CONSUMER=false

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
  echo -e "${BLUE}K3s Installer — v0.9.4-alpha${NC}"
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
  warn "This will delete namespace '${ns}' and ALL data within it."

  if [[ "${NO_PROMPTS}" == "true" ]]; then
    # Web wizard already confirmed — proceed automatically
    info "Auto-confirmed via web installer"
  else
    echo -en "${RED}Type namespace to confirm [${ns}]: ${NC}"; read -r confirm
    [[ "${confirm}" != "${ns}" ]] && { info "Cancelled."; exit 0; }
  fi

  info "Deleting namespace ${ns}..."
  kubectl delete namespace "${ns}" --ignore-not-found=true
  local issuer; issuer="$(yaml_get patchpilot.network.tls.clusterIssuer letsencrypt-prod)"
  kubectl delete clusterissuer "${issuer}" --ignore-not-found=true 2>/dev/null || true

  # Also clean up hostPath data on the node
  local node node_ip
  node="$(kubectl get nodes --field-selector='spec.unschedulable!=true' \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
  node_ip="$(kubectl get node "${node}" \
    -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)"

  if [[ -n "${node_ip}" ]]; then
    warn "You may want to clean leftover hostPath data on the node:"
    warn "  ssh ${node_ip} sudo rm -rf /app-data/patchpilot-*"
    # Emit structured marker so web wizard can surface this as a post-uninstall note
    echo "__NOTE_CLEANUP__ ssh ${node_ip} sudo rm -rf /app-data/patchpilot-*"
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

  PP_VERSION="$(yaml_get patchpilot.version 0.9.4-alpha)"
  PP_NAMESPACE="$(yaml_get patchpilot.namespace patchpilot)"
  PP_NAMESPACE="$(prompt_value "Kubernetes namespace" "${PP_NAMESPACE}")"

  # ── Image config — arch-suffixed tags to prevent arm/amd64 collision ─────
  PP_DH_REPO="$(yaml_get patchpilot.image.dockerHubRepo linit01/patchpilot)"
  PP_DH_REPO="$(prompt_value "Docker Hub repo" "${PP_DH_REPO}" true)"
  PP_DH_REPO="${PP_DH_REPO%/}"

  PP_IMAGE_STRATEGY="$(yaml_get patchpilot.image.strategy registry)"
  PP_IMAGE_TAG="$(yaml_get patchpilot.image.tag 0.9.4-alpha)"
  PP_IMAGE_PULL_POLICY="$(yaml_get patchpilot.image.pullPolicy Always)"
  PP_PULL_SECRET_NAME="$(yaml_get patchpilot.image.pullSecretName dockerhub-pull-secret)"

  # Detect arch NOW so we can bake it into the image tags
  # This prevents arm64 Docker builds from overwriting amd64 k3s images (and vice versa)
  # Tags become: linit01/patchpilot:backend-0.9.4-alpha-amd64
  local target_platform
  target_platform="$(detect_target_platform)"
  PP_TARGET_ARCH="$(echo "${target_platform}" | sed 's|linux/||;s|/|-|')"  # amd64, arm64, arm-v7
  info "Cluster architecture: ${PP_TARGET_ARCH}"

  PP_BACKEND_IMAGE="${PP_DH_REPO}:backend-${PP_IMAGE_TAG}-${PP_TARGET_ARCH}"
  PP_FRONTEND_IMAGE="${PP_DH_REPO}:frontend-${PP_IMAGE_TAG}-${PP_TARGET_ARCH}"
  info "Backend image:  ${PP_BACKEND_IMAGE}"
  info "Frontend image: ${PP_FRONTEND_IMAGE}"

  # ── Docker Hub credentials ─────────────────────────────────────────────────
  PP_DH_USERNAME="$(yaml_get patchpilot.dockerHub.username)"
  PP_DH_TOKEN="$(yaml_get patchpilot.dockerHub.token)"

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
  [[ -z "${PP_DH_USERNAME}" || -z "${PP_DH_TOKEN}" ]] && die "Docker Hub credentials required."

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
  if [[ "${PP_TLS_ENABLED}" == "true" ]]; then
    PP_LE_EMAIL="$(prompt_value "Let's Encrypt email" "${PP_LE_EMAIL}" true)"
    [[ "${PP_CHALLENGE_TYPE}" == "dns01-cloudflare" ]] && \
      PP_CF_EMAIL="$(prompt_value "Cloudflare account email" "${PP_CF_EMAIL}" true)"
  fi

  # ── Database ───────────────────────────────────────────────────────────────
  PP_DB_USER="$(yaml_get patchpilot.postgres.user patchpilot)"
  PP_DB_PASSWORD="$(yaml_get patchpilot.postgres.password)"
  PP_DB_NAME="$(yaml_get patchpilot.postgres.database patchpilot)"
  PP_POSTGRES_STORAGE_SIZE="$(yaml_get patchpilot.postgres.storageSize 5Gi)"
  PP_POSTGRES_STORAGE_CLASS="$(yaml_get patchpilot.postgres.storageClass local-data)"
  if [[ -z "${PP_DB_PASSWORD}" ]]; then
    PP_DB_PASSWORD="$(gen_password)"
    warn "Auto-generated PostgreSQL password: ${YELLOW}${PP_DB_PASSWORD}${NC} — save this"
  fi

  # ── Encryption key ─────────────────────────────────────────────────────────
  PP_ENCRYPTION_KEY="$(yaml_get patchpilot.app.encryptionKey)"
  if [[ -z "${PP_ENCRYPTION_KEY}" ]]; then
    PP_ENCRYPTION_KEY="$(gen_fernet_key)"
    warn "Auto-generated Fernet key: ${YELLOW}${PP_ENCRYPTION_KEY}${NC} — save this"
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
  PP_POSTGRES_STORAGE_CLASS="$(yaml_get patchpilot.postgres.storageClass local-data)"
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

  # ── Build PV source blocks ─────────────────────────────────────────────────
  _build_pv_source() {
    local sc_name="$1" pv_name="$2"
    if [[ -z "${sc_name}" ]]; then
      echo "  hostPath:"; echo "    path: /app-data/${pv_name}"; echo "    type: DirectoryOrCreate"
      return
    fi
    local provisioner
    provisioner="$(kubectl get sc "${sc_name}" -o jsonpath='{.provisioner}' 2>/dev/null)"
    case "${provisioner}" in
      rancher.io/local-path)
        local node
        node="$(kubectl get nodes --field-selector='spec.unschedulable!=true' \
          -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
        echo "  hostPath:"; echo "    path: /app-data/${pv_name}"; echo "    type: DirectoryOrCreate"
        echo "  nodeAffinity:"; echo "    required:"; echo "      nodeSelectorTerms:"
        echo "      - matchExpressions:"
        echo "        - key: kubernetes.io/hostname"; echo "          operator: In"
        echo "          values:"; echo "          - ${node}"
        ;;
      nfs.csi.k8s.io|nfs-subdir-external-provisioner|cluster.local/*)
        echo "  mountOptions:"; echo "  - nfsvers=3"; echo "  - hard"
        echo "  nfs:"; echo "    server: ${PP_NFS_SERVER}"; echo "    path: ${PP_NFS_SHARE}"
        ;;
      *)
        warn "Unknown provisioner '${provisioner}' for SC '${sc_name}' — falling back to hostPath"
        echo "  hostPath:"; echo "    path: /app-data/${pv_name}"; echo "    type: DirectoryOrCreate"
        ;;
    esac
  }

  PP_POSTGRES_PV_SOURCE="$(_build_pv_source "${PP_POSTGRES_STORAGE_CLASS}" "patchpilot-postgres-data")"
  PP_APP_PV_SOURCE="$(_build_pv_source "${PP_APP_STORAGE_CLASS}" "patchpilot-backups")"
  PP_APP_PV_SOURCE_ANSIBLE="$(_build_pv_source "${PP_ANSIBLE_STORAGE_CLASS}" "patchpilot-ansible-data")"

  # ── Ansible ────────────────────────────────────────────────────────────────
  PP_ANSIBLE_PLAYBOOK_PATH="$(yaml_get patchpilot.ansible.playbookPath)"
  PP_ANSIBLE_INVENTORY_PATH="$(yaml_get patchpilot.ansible.inventoryPath)"

  # ── Summary ────────────────────────────────────────────────────────────────
  echo ""
  echo -e "${CYAN}Configuration summary:${NC}"
  echo "  Namespace      : ${PP_NAMESPACE}"
  echo "  Version        : ${PP_VERSION}"
  echo "  Cluster arch   : ${PP_TARGET_ARCH}"
  echo "  Backend image  : ${PP_BACKEND_IMAGE}"
  echo "  Frontend image : ${PP_FRONTEND_IMAGE}"
  echo "  Primary host   : ${PP_HOSTNAME}"
  echo "  TLS enabled    : ${PP_TLS_ENABLED}"
  echo "  Backup storage : ${PP_BACKUP_STORAGE_TYPE}${PP_NFS_SERVER:+ (${PP_NFS_SERVER}:${PP_NFS_SHARE})}"
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
    ok "Images already pushed during buildx build — skipping"
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
  kubectl create secret docker-registry "${PP_PULL_SECRET_NAME}" \
    --namespace="${PP_NAMESPACE}" \
    --docker-server="https://index.docker.io/v1/" \
    --docker-username="${PP_DH_USERNAME}" \
    --docker-password="${PP_DH_TOKEN}" \
    --dry-run=client -o yaml | kubectl apply -f -
  ok "imagePullSecret '${PP_PULL_SECRET_NAME}' ready"
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
           PP_POSTGRES_PV_SOURCE PP_APP_PV_SOURCE PP_APP_PV_SOURCE_ANSIBLE \
           PP_POSTGRES_STORAGE_CLASS PP_APP_STORAGE_CLASS \
           PP_ANSIBLE_STORAGE_CLASS PP_ANSIBLE_STORAGE_CLASS_SPEC \
           PP_NFS_SERVER PP_NFS_SHARE PP_BACKUP_STORAGE_TYPE \
           PP_CLUSTER_ISSUER PP_TLS_SECRET_NAME PP_INGRESS_CLASS \
           PP_LE_EMAIL PP_CF_EMAIL PP_CF_API_TOKEN_SECRET
    envsubst '$PP_NAMESPACE:$PP_VERSION:$PP_BACKEND_IMAGE:$PP_FRONTEND_IMAGE:'\
'$PP_IMAGE_PULL_POLICY:$PP_PULL_SECRET_NAME:$PP_DB_USER:$PP_DB_PASSWORD:'\
'$PP_DB_NAME:$PP_ENCRYPTION_KEY:$PP_BASE_URL:$PP_ALLOWED_ORIGINS:'\
'$PP_POSTGRES_STORAGE_SIZE:$PP_BACKUPS_STORAGE_SIZE:$PP_ANSIBLE_STORAGE_SIZE:'\
'$PP_POSTGRES_PV_SOURCE:$PP_APP_PV_SOURCE:$PP_APP_PV_SOURCE_ANSIBLE:'\
'$PP_POSTGRES_STORAGE_CLASS:$PP_POSTGRES_STORAGE_CLASS_SPEC:'\
'$PP_APP_STORAGE_CLASS:$PP_APP_STORAGE_CLASS_SPEC:'\
'$PP_ANSIBLE_STORAGE_CLASS:$PP_ANSIBLE_STORAGE_CLASS_SPEC:'\
'$PP_NFS_SERVER:$PP_NFS_SHARE:$PP_BACKUP_STORAGE_TYPE:'\
'$PP_CLUSTER_ISSUER:$PP_TLS_SECRET_NAME:$PP_INGRESS_CLASS:'\
'$PP_LE_EMAIL:$PP_CF_EMAIL:$PP_CF_API_TOKEN_SECRET:'\
'$PP_AUTO_REFRESH_INTERVAL:$PP_DEFAULT_SSH_USER:$PP_DEFAULT_SSH_PORT:'\
'$PP_BACKUP_RETAIN_COUNT:$PP_MAX_BACKUP_SIZE_MB:'\
'$PP_INGRESS_RULES:$PP_TLS_HOSTS:$PP_TLS_DNS_NAMES:'\
'$PP_INGRESS_MIDDLEWARE_ANNOTATION:$PP_HOSTNAME' \
      < "$1" > "$2"
  }

  render "${tmpl}/00-namespace.yaml"  "${GENERATED_DIR}/00-namespace.yaml";  ok "00-namespace.yaml"
  render "${tmpl}/01-secrets.yaml"    "${GENERATED_DIR}/01-secrets.yaml";    ok "01-secrets.yaml"
  render "${tmpl}/02-pvs.yaml"        "${GENERATED_DIR}/02-pvs.yaml";        ok "02-pvs.yaml"
  render "${tmpl}/02b-pvcs.yaml"      "${GENERATED_DIR}/02b-pvcs.yaml";      ok "02b-pvcs.yaml"
  render "${tmpl}/03-postgres.yaml"   "${GENERATED_DIR}/03-postgres.yaml";   ok "03-postgres.yaml"
  render "${tmpl}/04-backend.yaml"    "${GENERATED_DIR}/04-backend.yaml";    ok "04-backend.yaml"
  render "${tmpl}/05-frontend.yaml"   "${GENERATED_DIR}/05-frontend.yaml";   ok "05-frontend.yaml"

  if [[ "${PP_TLS_ENABLED}" == "true" ]]; then
    render "${tmpl}/06-middlewares-https.yaml" "${GENERATED_DIR}/06-middlewares.yaml"
    ok "06-middlewares.yaml (HTTPS)"
    local dns_names=""
    for h in "${ALL_HOSTNAMES[@]}"; do dns_names+="    - ${h}"$'\n'; done
    export PP_TLS_DNS_NAMES="${dns_names%$'\n'}"
    render "${tmpl}/07-certificate.yaml" "${GENERATED_DIR}/07-certificate.yaml"
    ok "07-certificate.yaml"
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

    render "${tmpl}/08-ingress-https.yaml" "${GENERATED_DIR}/08-ingress.yaml"
    ok "08-ingress.yaml (HTTPS)"
  else
    render "${tmpl}/08-ingress-http.yaml" "${GENERATED_DIR}/08-ingress.yaml"
    ok "08-ingress.yaml (HTTP)"
  fi

  if [[ "${PP_TLS_ENABLED}" == "true" && "${PP_CREATE_CLUSTER_ISSUER}" == "true" ]]; then
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

  if [[ -n "${PP_ANSIBLE_PLAYBOOK_PATH:-}" || -n "${PP_ANSIBLE_INVENTORY_PATH:-}" ]]; then
    generate_ansible_configmap
  fi

  ok "All manifests in: ${GENERATED_DIR}/"
}

# ── Ansible ConfigMap ──────────────────────────────────────────────────────────
generate_ansible_configmap() {
  info "Generating Ansible ConfigMap..."
  local cm="${GENERATED_DIR}/10-ansible-configmap.yaml"
  cat > "${cm}" << YAML
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: patchpilot-ansible-init
  namespace: ${PP_NAMESPACE}
data:
YAML
  if [[ -n "${PP_ANSIBLE_PLAYBOOK_PATH}" && -f "${PP_ANSIBLE_PLAYBOOK_PATH}" ]]; then
    echo "  check-os-updates.yml: |" >> "${cm}"
    sed 's/^/    /' "${PP_ANSIBLE_PLAYBOOK_PATH}" >> "${cm}"
    ok "Playbook included in ConfigMap"
  fi
  if [[ -n "${PP_ANSIBLE_INVENTORY_PATH}" && -f "${PP_ANSIBLE_INVENTORY_PATH}" ]]; then
    echo "  hosts: |" >> "${cm}"
    sed 's/^/    /' "${PP_ANSIBLE_INVENTORY_PATH}" >> "${cm}"
    ok "Inventory included in ConfigMap"
  fi
  cat >> "${cm}" << YAML
---
apiVersion: batch/v1
kind: Job
metadata:
  name: patchpilot-ansible-init
  namespace: ${PP_NAMESPACE}
spec:
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: OnFailure
      containers:
      - name: copy-ansible
        image: busybox:1.36
        command: ["sh","-c","cp /cm/check-os-updates.yml /ansible/ 2>/dev/null; cp /cm/hosts /ansible/ 2>/dev/null; echo done; ls /ansible/"]
        volumeMounts:
        - name: ansible-data
          mountPath: /ansible
        - name: ansible-cm
          mountPath: /cm
          readOnly: true
      volumes:
      - name: ansible-data
        persistentVolumeClaim:
          claimName: patchpilot-ansible-data
      - name: ansible-cm
        configMap:
          name: patchpilot-ansible-init
YAML
  ok "Generated: 10-ansible-configmap.yaml"
}

# ── Validate StorageClasses ────────────────────────────────────────────────────
validate_storage_classes() {
  step "Validating StorageClasses"
  local available_scs
  available_scs="$(kubectl get sc -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
  for sc_var in "${PP_POSTGRES_STORAGE_CLASS}" "${PP_APP_STORAGE_CLASS}"; do
    [[ -z "${sc_var}" ]] && continue
    if echo "${available_scs}" | tr ' ' '\n' | grep -qx "${sc_var}"; then
      ok "StorageClass exists: ${sc_var}"
      local binding_mode
      binding_mode="$(kubectl get sc "${sc_var}" -o jsonpath='{.volumeBindingMode}' 2>/dev/null)"
      if [[ "${binding_mode}" == "WaitForFirstConsumer" ]]; then
        warn "StorageClass '${sc_var}' uses WaitForFirstConsumer — PVCs will pend until pod schedules"
        PP_SC_WAIT_FOR_CONSUMER="true"
      fi
    else
      err "StorageClass '${sc_var}' not found in cluster"
      kubectl get sc --no-headers 2>/dev/null | awk '{print "    " $1 "  (" $2 ")"}' >&2
      die "StorageClass validation failed — fix config and retry."
    fi
  done
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
  local node node_ip
  node="$(kubectl get nodes --field-selector='spec.unschedulable!=true' \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
  node_ip="$(kubectl get node "${node}" \
    -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)"

  [[ -z "${node}" ]] && return 0

  step "Node data cleanup — ${node} (${node_ip})"

  local cleanup_cmd="ssh ${node_ip} sudo rm -rf /app-data/patchpilot-*"

  # ── Check whether stale dirs actually exist before doing anything ──────────
  # Use BatchMode=yes (no password, no TTY) — we are only doing a read-only
  # directory listing, not sudo. If the check itself fails (SSH not reachable,
  # no key auth) we treat it as "unknown" and still pause to be safe.
  local dirs_exist="unknown"
  local ssh_target="${node_ip}"

  # Try by IP first, then by hostname
  for _target in "${node_ip}" "${node}"; do
    if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes         "${_target}" "test -d /app-data" 2>/dev/null; then
      ssh_target="${_target}"
      # Count matching entries; exit 0 = found, exit 1 = none
      if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes           "${_target}" "find /app-data -maxdepth 1 -name 'patchpilot-*' -print -quit 2>/dev/null | grep -q ." 2>/dev/null; then
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
    warn "Stale /app-data/patchpilot-* found on node — must be removed before postgres can init."
  fi

  # Update cleanup_cmd to use the reachable target if we found one
  [[ "${dirs_exist}" == "yes" ]] && cleanup_cmd="ssh ${ssh_target} sudo rm -rf /app-data/patchpilot-*"

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

  # ── Apply each manifest, showing full kubectl output ─────────────────────
  step "Applying manifests"
  for manifest in "${GENERATED_DIR}"/*.yaml; do
    local base; base="$(basename "${manifest}")"
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
  [[ "${PP_TLS_ENABLED}" == "true" ]] && \
    echo -e "${YELLOW}⏳ TLS:${NC} Certificate may take 1–3 min via Let's Encrypt."
  echo ""
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
  clean_node_data_dirs
  apply_manifests
  wait_for_rollout
  show_completion
}

main "$@"
