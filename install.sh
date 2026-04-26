#!/usr/bin/env bash
set -e

# PatchPilot — Installer (version read from VERSION file)
# Supports: Docker Compose  |  K3s (Kubernetes)  |  Web Wizard

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PP_FILE_VERSION="$(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]')"
PP_FILE_VERSION="${PP_FILE_VERSION:-0.0.0-dev}"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; PURPLE='\033[0;35m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }
step() { echo ""; echo -e "${PURPLE}▸${NC} $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

# ── Cross-platform sed -i (macOS BSD vs Linux GNU) ────────────────────────────
# Usage: sed_i 's/foo/bar/' file
sed_i() { sed -i '' "$@" 2>/dev/null || sed -i "$@"; }

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
  echo -e "${BLUE}System Update Management — v${PP_FILE_VERSION}${NC}"
  echo ""
}

# ── Argument parsing ──────────────────────────────────────────────────────────
MODE=""
NO_INTERACTIVE=false
DEVELOPER_MODE=false
WEB_PORT=9090
# Preserved before the parse loop consumes "$@" (used for sg docker re-exec)
ORIG_INSTALLER_ARGS=("$@")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker)          MODE="docker"; shift ;;
    --k3s)             MODE="k3s";    shift ;;
    --web)             MODE="web";    shift ;;
    --developer)       DEVELOPER_MODE=true; shift ;;
    --no-interactive)  NO_INTERACTIVE=true; shift ;;
    --port)            WEB_PORT="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: ./install.sh [--docker | --k3s | --web] [OPTIONS]"
      echo ""
      echo "  --docker           Install using Docker Compose (pulls published images)"
      echo "  --docker --developer  Build images from local source (docker-compose.developer.yml)"
      echo "  --k3s              Install on a K3s/Kubernetes cluster"
      echo "  --web              Launch web-based install wizard (http://localhost:9090)"
      echo "  --web --developer  Web wizard + Developer tab (build & push images first)"
      echo "  --no-interactive   Skip all prompts — config must exist in k8s/install-config.yaml"
      echo "  --port N           Web wizard port (default: 9090)"
      exit 0
      ;;
    *) err "Unknown argument: $1"; exit 1 ;;
  esac
done

export NO_INTERACTIVE

# ── Sudo helper (web installer sets PATCHPILOT_SUDO_PASSFILE → non-TTY sudo -S) ─
_PP_SUDO_PASS=""
if [[ -n "${PATCHPILOT_SUDO_PASSFILE:-}" ]] && [[ -f "${PATCHPILOT_SUDO_PASSFILE}" ]]; then
  IFS= read -r _PP_SUDO_PASS < "${PATCHPILOT_SUDO_PASSFILE}" || true
fi
pp_sudo() {
  if [[ -n "${_PP_SUDO_PASS}" ]]; then
    printf '%s\n' "${_PP_SUDO_PASS}" | sudo -S -p '' "$@"
  else
    sudo "$@"
  fi
}

# ── Sanity checks ─────────────────────────────────────────────────────────────
ensure_curl() {
  if ! command -v curl &>/dev/null; then
    if [[ -f /etc/debian_version ]]; then
      info "Installing curl..."
      DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get update -qq
      DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get install -y -qq curl
    else
      err "curl is required but not installed."; exit 1
    fi
  fi
}

check_not_root() {
  if [[ "$EUID" -eq 0 ]]; then
    err "Please don't run this installer as root."
    exit 1
  fi
}

# ── Mode selection ────────────────────────────────────────────────────────────
select_mode() {
  [[ -n "${MODE}" ]] && return

  # Default to web wizard when no flag is specified
  # Users can still pass --docker or --k3s directly
  echo -e "${CYAN}How would you like to install PatchPilot?${NC}"
  echo ""
  echo "  1) ${PURPLE}Web Wizard${NC}       — browser-based guided install (recommended)"
  echo "  2) ${GREEN}Docker Compose${NC}  — single host CLI install"
  echo "  3) ${BLUE}K3s / Kubernetes${NC} — cluster deployment with Traefik + cert-manager"
  echo ""
  local choice=""
  while [[ "${choice}" != "1" && "${choice}" != "2" && "${choice}" != "3" ]]; do
    echo -en "${CYAN}Choose [1/2/3] (default: 1): ${NC}"
    read -r choice
    [[ -z "${choice}" ]] && choice="1"
  done
  case "${choice}" in
    1) MODE="web" ;;
    2) MODE="docker" ;;
    3) MODE="k3s" ;;
  esac
}

# ═════════════════════════════════════════════════════════════════════════════
# WEB WIZARD
# ═════════════════════════════════════════════════════════════════════════════
install_web() {
  step "Starting PatchPilot Web Installer"
  local web_dir="${SCRIPT_DIR}/webinstall"
  local venv_dir="${web_dir}/.venv"
  local req_file="${web_dir}/requirements.txt"

  [[ -d "${web_dir}" ]] || { err "webinstall/ not found"; exit 1; }
  [[ -f "${req_file}" ]] || { err "webinstall/requirements.txt not found"; exit 1; }

  # Ensure python3 is available — install on Debian/Ubuntu if missing
  if ! command -v python3 &>/dev/null; then
    if [[ -f /etc/debian_version ]]; then
      info "Installing python3..."
      DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get update -qq
      DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get install -y -qq python3 python3-venv
    else
      err "python3 is required but not installed."; exit 1
    fi
  fi

  # Debian/Ubuntu split python3-venv into its own package — install if missing
  if [[ -f /etc/debian_version ]] && ! python3 -c "import venv" &>/dev/null; then
    info "Installing python3-venv..."
    DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get update -qq
    DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get install -y -qq python3-venv
  fi

  # Isolated venv — avoids PEP 668 conflicts on Debian/macOS-Homebrew and
  # the package-skew problems caused by installing into system site-packages.
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    info "Creating Python virtualenv at ${venv_dir}..."
    rm -rf "${venv_dir}"
    python3 -m venv "${venv_dir}"
  fi

  info "Installing web installer dependencies into venv..."
  "${venv_dir}/bin/pip" install --quiet --upgrade pip
  "${venv_dir}/bin/pip" install --quiet -r "${req_file}"

  export PATCHPILOT_ROOT="${SCRIPT_DIR}"
  export PATCHPILOT_WEB_PORT="${WEB_PORT}"
  export PATCHPILOT_DEVELOPER="${DEVELOPER_MODE}"

  # Detect LAN IP for remote-access instructions (server installs)
  local lan_ip
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -z "${lan_ip}" ]] && lan_ip="<this-host-ip>"

  echo ""
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}  PatchPilot Web Installer${NC}"
  echo -e "${GREEN}  → http://localhost:${WEB_PORT}${NC}"
  echo -e "${GREEN}  → http://${lan_ip}:${WEB_PORT}  (remote access)${NC}"
  [[ "${DEVELOPER_MODE}" == "true" ]] && \
    echo -e "${YELLOW}  🔧 Developer mode enabled (Build & Push tab active)${NC}"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${YELLOW}  ⚠  Port ${WEB_PORT} is open on all interfaces during setup.${NC}"
  echo -e "${YELLOW}     Complete setup promptly and ensure your firewall${NC}"
  echo -e "${YELLOW}     restricts access to trusted hosts only.${NC}"
  echo -e "${YELLOW}  Press Ctrl+C to stop${NC}"
  echo ""

  sleep 1 && (
    open "http://localhost:${WEB_PORT}" 2>/dev/null || \
    xdg-open "http://localhost:${WEB_PORT}" 2>/dev/null || true
  ) &

  cd "${web_dir}" && "${venv_dir}/bin/python" -m uvicorn server:app \
    --host 0.0.0.0 --port "${WEB_PORT}" --log-level warning
}

# ═════════════════════════════════════════════════════════════════════════════
# DOCKER COMPOSE INSTALL
# ═════════════════════════════════════════════════════════════════════════════
DOCKER_COMPOSE_CMD=""

# Re-run this script with an active 'docker' group (Linux socket ACL)
docker_reexec_under_docker_group() {
  local q_script q_args
  q_script=$(printf '%q' "${BASH_SOURCE[0]}")
  q_args=$(printf '%q ' "${ORIG_INSTALLER_ARGS[@]}")
  exec sg docker -c "bash ${q_script} ${q_args}"
}

# Linux: prompt for sudo once before Docker Engine/group work so the password
# is not a surprise after "prerequisites" lines. Skipped if docker info already works.
docker_prime_sudo_for_linux_if_needed() {
  [[ "$(uname -s)" == "Linux" ]] || return 0
  if docker info &>/dev/null; then
    return 0
  fi

  step "Administrator access (sudo)"
  info "Docker is not usable for this account yet (not installed, daemon stopped, or user not in the 'docker' group)."
  info "This installer uses sudo to install or configure Docker Engine and to add you to the 'docker' group."

  # Web installer: password supplied via PATCHPILOT_SUDO_PASSFILE (non-TTY)
  if [[ -n "${_PP_SUDO_PASS}" ]]; then
    info "Using the sudo password you entered in the web installer."
    if ! pp_sudo -v; then
      err "That sudo password was rejected (wrong password or user not in sudoers)."
      exit 1
    fi
    ok "sudo session ready"
    return 0
  fi

  info "Enter your password when prompted — sudo keeps it cached for several minutes."

  if ! [ -t 0 ] && ! sudo -n true 2>/dev/null; then
    err "A sudo password needs an interactive terminal, or use the PatchPilot web installer and enter sudo there."
    echo "  From a shell, download then run, for example:" >&2
    echo "    curl -fsSL https://getpatchpilot.app/install.sh -o install.sh && bash install.sh" >&2
    exit 1
  fi

  pp_sudo -v || { err "sudo is required on Linux until Docker works for this user."; exit 1; }
  ok "sudo session ready"
}

docker_ensure_daemon_access() {
  if docker info &>/dev/null; then
    return 0
  fi
  local out
  out="$(docker info 2>&1 || true)"

  local is_linux=false
  [[ "$(uname -s)" == "Linux" ]] && is_linux=true

  local is_perm_denied=false
  if [[ "$out" == *"permission denied"* ]] || [[ "$out" == *"denied while trying to connect"* ]]; then
    is_perm_denied=true
  fi

  # Linux Docker Engine only: root:docker socket → usermod + sg (never on macOS/Windows/Git Bash)
  if [[ "${is_linux}" == true && "${is_perm_denied}" == true ]]; then
    local in_group=false
    id -Gn "$USER" 2>/dev/null | grep -wq docker && in_group=true

    if [[ "${in_group}" == true ]]; then
      if [[ "${PATCHPILOT_DOCKER_SG_REEXEC:-}" == "1" ]]; then
        err "Still cannot access Docker at unix:///var/run/docker.sock."
        echo "  Check:  ls -l /var/run/docker.sock" >&2
        echo "  Or start a new login session, or:  newgrp docker" >&2
        echo "  Then re-run this installer with the same options." >&2
        exit 1
      fi
      export PATCHPILOT_DOCKER_SG_REEXEC=1
      warn "Docker socket permission denied — re-running installer under active 'docker' group..."
      docker_reexec_under_docker_group
    fi

    pp_sudo usermod -aG docker "$USER"
    warn "Added '${USER}' to the 'docker' group (required for /var/run/docker.sock)."
    warn "Re-running the installer with that group active in this session..."
    docker_reexec_under_docker_group
  fi

  # Any OS: same generic error (no usermod/sg/systemd assumptions)
  err "Cannot reach the Docker daemon."
  echo "$out" | head -10 >&2
  if [[ "${is_linux}" == true ]]; then
    if [[ "$out" == *"Cannot connect"* ]] || [[ "$out" == *"Is the docker daemon running"* ]]; then
      echo "" >&2
      echo "  Try:  sudo systemctl start docker" >&2
    fi
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    echo "" >&2
    echo "  On macOS: start Docker Desktop and wait until Docker is running." >&2
  fi
  exit 1
}

docker_install_engine() {
  step "Checking Docker Engine"

  # ── Detect Docker Desktop on Linux (incompatible socket path) ─────────────
  # Linux only: Docker Desktop's VM-backed socket conflicts with the compose
  # stack which expects the standard /var/run/docker.sock from Docker Engine.
  # On macOS/Windows, Docker Desktop is the standard way to run Docker and
  # works correctly with this stack — don't reject it there.
  if [[ "$(uname -s)" == "Linux" ]] && command -v docker &>/dev/null; then
    local ctx
    ctx="$(docker context show 2>/dev/null || true)"
    if [[ "${ctx}" == "desktop-linux" ]] || \
       docker info 2>/dev/null | grep -q "Docker Desktop"; then
      err "Docker Desktop detected — PatchPilot requires Docker Engine on Linux."
      echo ""
      echo "  Docker Desktop on Linux uses a VM-backed socket that is incompatible"
      echo "  with PatchPilot's compose stack."
      echo ""
      echo "  To fix:"
      echo "    1. Uninstall Docker Desktop"
      echo "    2. Install Docker Engine:  https://docs.docker.com/engine/install/ubuntu/"
      echo "    3. Re-run this installer"
      exit 1
    fi
  fi

  # ── Already have Docker Engine + compose plugin → skip ────────────────────
  if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    ok "Docker Engine already installed: $(docker --version)"
    ok "Docker Compose plugin already installed"
    return
  fi

  # ── Only auto-install on Debian/Ubuntu ────────────────────────────────────
  if [[ ! -f /etc/debian_version ]]; then
    case "$(uname -s)" in
      Darwin)
        warn "Docker is required but not installed."
        warn "Install Docker Desktop for Mac: https://docs.docker.com/desktop/install/mac-install/"
        warn "(Colima also works: https://github.com/abiosoft/colima)"
        ;;
      *)
        warn "Auto-install only supported on Debian/Ubuntu."
        warn "Install Docker Engine manually: https://docs.docker.com/engine/install/"
        ;;
    esac
    return
  fi

  info "Installing Docker Engine from official Docker apt repository..."

  # Remove stale/distro packages that conflict with official Docker Engine
  local stale_pkgs=(docker.io docker-doc docker-compose docker-compose-v2
                    podman-docker containerd runc)
  for pkg in "${stale_pkgs[@]}"; do
    dpkg -l "${pkg}" &>/dev/null 2>&1 && \
      DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get remove -y "${pkg}" &>/dev/null || true
  done

  DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get update -qq
  DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get install -y -qq ca-certificates curl gnupg

  # Add official Docker GPG key
  pp_sudo install -m 0755 -d /etc/apt/keyrings
  pp_sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  pp_sudo chmod a+r /etc/apt/keyrings/docker.asc

  # Add Docker apt repository
  local distro_id distro_codename
  distro_id="$(. /etc/os-release && echo "${ID}")"
  distro_codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"

  # Linux Mint and other Ubuntu derivatives report their own codename —
  # map back to the upstream Ubuntu codename via UBUNTU_CODENAME if present
  local ubuntu_codename
  ubuntu_codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-}")"
  [[ -n "${ubuntu_codename}" ]] && distro_codename="${ubuntu_codename}"

  # Debian uses its own repo path; Ubuntu/derivatives use ubuntu
  local repo_distro="ubuntu"
  [[ "${distro_id}" == "debian" ]] && repo_distro="debian"

  # Cannot pipe into `pp_sudo tee` when sudo uses -S (stdin is the password).
  local docker_list_tmp
  docker_list_tmp="$(mktemp)"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${repo_distro} ${distro_codename} stable" > "${docker_list_tmp}"
  pp_sudo cp "${docker_list_tmp}" /etc/apt/sources.list.d/docker.list
  rm -f "${docker_list_tmp}"

  DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get update -qq
  DEBIAN_FRONTEND=noninteractive pp_sudo -E apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

  ok "Docker Engine installed: $(docker --version)"
  ok "Docker Compose plugin installed: $(docker compose version)"

  # ── Enable and start Docker service ───────────────────────────────────────
  pp_sudo systemctl enable docker --quiet
  pp_sudo systemctl start docker
  ok "Docker service enabled and started"

  # ── Add current user to docker group ──────────────────────────────────────
  if ! groups "${USER}" | grep -q '\bdocker\b'; then
    pp_sudo usermod -aG docker "${USER}"
    warn "User '${USER}' added to 'docker' group."
    warn "Group membership takes effect on next login."
    warn "Re-executing installer under new group membership for this session..."
    docker_reexec_under_docker_group
  fi
}

docker_check_prerequisites() {
  step "Checking Docker prerequisites"
  local missing=()
  ! command -v docker &>/dev/null && missing+=("docker") && err "Docker not installed" \
    || ok "Docker: $(docker --version)"
  if docker compose version &>/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD="docker compose"; ok "Docker Compose plugin: found"
  elif command -v docker-compose &>/dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"; ok "Docker Compose legacy: found"
  else
    missing+=("docker-compose"); err "Docker Compose not installed"
  fi
  [[ ${#missing[@]} -gt 0 ]] && { err "Missing: ${missing[*]}"; exit 1; }
  docker_ensure_daemon_access
  ok "All Docker prerequisites satisfied"
}

docker_setup_env() {
  step "Configuring environment"
  if [[ -f ".env" ]]; then info "Existing .env found — skipping"; return; fi
  [[ -f ".env.example" ]] || { err ".env.example not found"; exit 1; }
  cp .env.example .env
  local fernet_key
  fernet_key="$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || \
                python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")"
  sed_i "s|PATCHPILOT_ENCRYPTION_KEY=CHANGE_ME_FERNET_KEY|PATCHPILOT_ENCRYPTION_KEY=${fernet_key}|" .env
  sed_i "s|INSTALL_DIR=/path/to/patchpilot|INSTALL_DIR=${SCRIPT_DIR}|" .env
  warn "Auto-generated Fernet key — saved to .env — keep this safe"
  ok "Environment configured — INSTALL_DIR set to ${SCRIPT_DIR}"
}

docker_setup_ansible() {
  step "Setting up Ansible configuration"
  mkdir -p ansible
  for file_type in "playbook:check-os-updates.yml:check-os-updates.yml" "inventory:hosts:hosts"; do
    IFS=':' read -r type filename target <<< "${file_type}"
    [[ -f "ansible/${target}" ]] && { info "Existing Ansible ${type} found"; continue; }
    local found=""
    for p in "$HOME/${filename}" "$HOME/ansible/${filename}" "$HOME/Scripts/${filename}"; do
      [[ -f "${p}" ]] && { found="${p}"; break; }
    done
    if [[ -n "${found}" ]]; then
      cp "${found}" "ansible/${target}"; ok "Copied ${type} from ${found}"
    elif [[ "${NO_INTERACTIVE}" != "true" ]]; then
      echo -en "${CYAN}Path to Ansible ${type} [skip]: ${NC}"; read -r user_path
      [[ -n "${user_path}" && -f "${user_path}" ]] && cp "${user_path}" "ansible/${target}" \
        && ok "Copied Ansible ${type}" || warn "Ansible ${type} not set"
    else
      warn "Ansible ${type} not set — configure later in ./ansible/"
    fi
  done
  ok "Ansible configuration ready"
}

docker_start_services() {
  step "Starting PatchPilot (Docker Compose)"
  local -a compose_files=(-f docker-compose.yml)
  if [[ "${DEVELOPER_MODE}" == "true" ]]; then
    compose_files+=(-f docker-compose.developer.yml)
    info "Developer mode: building images from local source..."
    $DOCKER_COMPOSE_CMD "${compose_files[@]}" build
  else
    info "Pulling pre-built images from registry..."
    $DOCKER_COMPOSE_CMD "${compose_files[@]}" pull
  fi
  info "Starting services..."
  $DOCKER_COMPOSE_CMD "${compose_files[@]}" up -d
  info "Waiting for backend to be ready..."
  local i=0
  until curl -sf http://localhost:8080/api/auth/check-setup >/dev/null 2>&1; do
    i=$((i+1))
    if [[ $i -ge 60 ]]; then
      warn "Backend didn't respond after 120s — check: ${DOCKER_COMPOSE_CMD} logs backend"
      return
    fi
    sleep 2
  done
  ok "Backend healthy"
  curl -sf http://localhost:8080/ >/dev/null 2>&1 && ok "Frontend healthy" \
    || warn "Frontend still starting — check: ${DOCKER_COMPOSE_CMD} logs frontend"
  sleep 1 && (
    local _oh
    _oh="$(docker_access_host)"
    open "http://${_oh}:8080" 2>/dev/null || \
    xdg-open "http://${_oh}:8080" 2>/dev/null || true
  ) &
}

# Host printed in Docker completion URLs — LAN IP when obvious, else localhost.
# Override with PATCHPILOT_ACCESS_HOST=10.0.1.111 if detection is wrong on your box.
docker_access_host() {
  if [[ -n "${PATCHPILOT_ACCESS_HOST:-}" ]]; then
    echo "${PATCHPILOT_ACCESS_HOST}"
    return
  fi
  local ip cand
  if [[ "$(uname -s)" == "Darwin" ]]; then
    cand="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    [[ -n "$cand" && "$cand" != "127.0.0.1" ]] && echo "$cand" && return
    echo "localhost"
    return
  fi
  # Linux: pick first non-loopback, non-docker0-style address from "hostname -I"
  for ip in $(hostname -I 2>/dev/null); do
    [[ "$ip" =~ ^127\. ]] && continue
    [[ "$ip" =~ ^169\.254\. ]] && continue
    [[ "$ip" =~ ^172\.17\. ]] && continue
    echo "$ip"
    return
  done
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$ip" && "$ip" != "127.0.0.1" ]] && echo "$ip" && return
  echo "localhost"
}

docker_show_completion() {
  local host
  host="$(docker_access_host)"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo -e "${GREEN}🎉  PatchPilot v${PP_FILE_VERSION} ready (Docker)!${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo -e "${BLUE}📊 Dashboard:${NC}  http://${host}:8080"
  echo -e "${BLUE}🔌 API:${NC}        http://${host}:8000"
  if [[ "$host" != "localhost" && "$host" != "127.0.0.1" ]]; then
    echo ""
    echo -e "${CYAN}  On this machine:${NC} http://localhost:8080"
  fi
  echo ""
  echo -e "${PURPLE}Commands:${NC}  ${DOCKER_COMPOSE_CMD} [logs -f | down | restart]"
  echo ""
}

install_docker() {
  cd "$SCRIPT_DIR"
  docker_prime_sudo_for_linux_if_needed
  docker_install_engine
  docker_check_prerequisites
  docker_setup_env
  docker_setup_ansible
  docker_start_services
  docker_show_completion
}

# ═════════════════════════════════════════════════════════════════════════════
# K3S INSTALL
# ═════════════════════════════════════════════════════════════════════════════
install_k3s() {
  step "Launching K3s installer"
  local k3s_script="${SCRIPT_DIR}/k8s/install-k3s.sh"
  [[ -f "${k3s_script}" ]] || { err "K3s installer not found: ${k3s_script}"; exit 1; }
  chmod +x "${k3s_script}"

  local config_file="${SCRIPT_DIR}/k8s/install-config.yaml"
  if [[ ! -f "${config_file}" ]]; then
    err "K3s config not found: ${config_file}"
    echo ""
    echo "Run the web wizard first:  ./install.sh --web"
    echo "Or create the config:      edit k8s/install-config.yaml"
    exit 1
  fi

  if [[ "${NO_INTERACTIVE}" != "true" ]]; then
    echo ""; info "Config: ${config_file}"
    echo -en "${CYAN}Continue with K3s install? [y/N]: ${NC}"; read -r confirm
    [[ "${confirm}" =~ ^[Yy]$ ]] || { info "Aborted."; exit 0; }
  fi

  local flags=""
  [[ "${NO_INTERACTIVE}" == "true" ]] && flags="--no-interactive"
  exec "${k3s_script}" ${flags}
}

# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════
main() {
  print_banner
  ensure_curl
  check_not_root
  select_mode
  case "${MODE}" in
    docker) install_docker ;;
    k3s)    install_k3s ;;
    web)    install_web ;;
    *)      err "Unknown mode: ${MODE}"; exit 1 ;;
  esac
}

main "$@"
