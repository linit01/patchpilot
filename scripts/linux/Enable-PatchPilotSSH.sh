#!/usr/bin/env bash
#
# Enable-PatchPilotSSH.sh — prepare a Linux host for PatchPilot management.
#
# Mirrors the Windows companion script (scripts/windows/Enable-PatchPilotSSH.ps1)
# by creating a dedicated, key-only `patchpilot` service account with NOPASSWD
# sudo scoped to the host's package manager + reboot/shutdown. macOS takes a
# different shape (operator's own user) because Apple ID / mas tie the patching
# user to the logged-in account; Linux has no such constraint and benefits
# from a least-privilege service account.
#
#   1. Detects the distro family from /etc/os-release
#   2. Creates a `patchpilot` system user (no password, key-only login)
#   3. Authorizes a PatchPilot SSH public key in ~patchpilot/.ssh/authorized_keys
#   4. Writes /etc/sudoers.d/patchpilot with NOPASSWD entries for the detected
#      package manager + /sbin/reboot, /sbin/shutdown + /bin/sh, /bin/bash
#      (the shells are required because Ansible's become wraps every task in
#      `sudo -n /bin/sh -c '...'`; effective blast radius is unchanged from
#      already allowing apt-get/dpkg/dnf which can install arbitrary packages)
#   5. Ensures sshd is running and enabled
#
# Run on each Linux host as root (curl-pipe with sudo, or invoke directly):
#
#   curl -fsSL https://<patchpilot-host>/scripts/linux/Enable-PatchPilotSSH.sh | \
#     sudo bash -s -- --public-key "ssh-ed25519 AAAA... patchpilot"
#
# Or download and run locally:
#
#   sudo ./Enable-PatchPilotSSH.sh --public-key "ssh-ed25519 AAAA... patchpilot"
#
# Supported distros: Debian/Ubuntu, RHEL family (RHEL/CentOS/Fedora/Rocky/Alma),
# SUSE/openSUSE, Arch. Anything else fails with a clear message.

set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────────────
PUBLIC_KEY=""
SERVICE_USER="patchpilot"
SKIP_SSHD=0

usage() {
    cat <<USAGE
Usage: $0 --public-key "ssh-ed25519 AAAA... patchpilot" [options]

Options:
  --public-key KEY        SSH public key to authorize (required)
  --service-user NAME     Service account name (default: patchpilot)
  --skip-sshd             Skip enabling/starting sshd
  -h, --help              Show this help

USAGE
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --public-key)   PUBLIC_KEY="$2"; shift 2 ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --skip-sshd)    SKIP_SSHD=1; shift ;;
        -h|--help)      usage 0 ;;
        *)              echo "Unknown arg: $1" >&2; usage 1 ;;
    esac
done

# ── Colors / helpers ─────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_RESET=""
fi

TOTAL_STEPS=5
step() { printf "\n%s[%s/%s] %s%s\n" "$C_CYAN" "$1" "$TOTAL_STEPS" "$2" "$C_RESET"; }
ok()   { printf "    %sOK:%s %s\n"   "$C_GREEN"  "$C_RESET" "$1"; }
skip() { printf "    %sSKIP:%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
warn() { printf "    %sWARN:%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
fail() { printf "    %sFAIL:%s %s\n" "$C_RED"    "$C_RESET" "$1" >&2; }
info() { printf "    %s%s%s\n"       "$C_DIM"    "$1" "$C_RESET"; }

if [[ "$(uname)" != "Linux" ]]; then
    fail "This script is for Linux. Detected: $(uname)."
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    fail "Must run as root (creating a system user, writing /etc/sudoers.d). Re-run with sudo."
    exit 1
fi

if [[ -z "$PUBLIC_KEY" ]]; then
    fail "--public-key is required."
    usage 1
fi

# Light validation: reject obviously-wrong keys early
if ! [[ "$PUBLIC_KEY" =~ ^(ssh-(rsa|ed25519|ed25519-sk|dss)|ecdsa-sha2-(nistp256|nistp384|nistp521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256))[[:space:]] ]]; then
    fail "--public-key does not look like a valid SSH public key (got: ${PUBLIC_KEY:0:40}...)"
    exit 1
fi

# ── Distro detection ─────────────────────────────────────────────────────────
# Read ID + ID_LIKE from /etc/os-release; pick the package-manager family.
DISTRO_FAMILY=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    # Build a haystack of ID + ID_LIKE so derivatives match (e.g. Pop_OS → ubuntu/debian).
    HAY=" ${ID:-} ${ID_LIKE:-} "
    case " $HAY " in
        *" debian "*|*" ubuntu "*)                      DISTRO_FAMILY="debian"  ;;
        *" rhel "*|*" centos "*|*" fedora "*|*" rocky "*|*" alma "*|*" ol "*) DISTRO_FAMILY="rhel" ;;
        *" suse "*|*" opensuse "*|*" sles "*)           DISTRO_FAMILY="suse"    ;;
        *" arch "*|*" manjaro "*)                       DISTRO_FAMILY="arch"    ;;
    esac
fi

if [[ -z "$DISTRO_FAMILY" ]]; then
    fail "Unsupported or undetectable distro. /etc/os-release ID='${ID:-?}' ID_LIKE='${ID_LIKE:-?}'."
    info "Supported families: debian/ubuntu, rhel/centos/fedora/rocky/alma, suse/opensuse, arch."
    exit 1
fi

HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"

printf "\n%sPatchPilot Linux host setup%s\n" "$C_CYAN" "$C_RESET"
info "Host:        $HOSTNAME_FQDN"
info "Distro:      ${ID:-?} ${VERSION_ID:-?} (family: $DISTRO_FAMILY)"
info "User:        $SERVICE_USER (will be created if missing)"
info "Sudoers:     /etc/sudoers.d/patchpilot"

# ── 1. Service user ──────────────────────────────────────────────────────────
step 1 "Creating service user '$SERVICE_USER'"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    ok "User '$SERVICE_USER' already exists"
else
    # --system: low UID, no aging; --create-home + bash for an interactive shell
    # the operator can `su - patchpilot` into for debugging. No password set →
    # passwd login is disabled; only the SSH key authorizes access.
    useradd --system --create-home --shell /bin/bash "$SERVICE_USER"
    # Lock the password explicitly (some distros leave it as '!' which is fine,
    # but '*' is the canonical disabled-login marker).
    passwd --lock "$SERVICE_USER" >/dev/null 2>&1 || true
    ok "User '$SERVICE_USER' created (no password, key-only login)"
fi

SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
if [[ -z "$SERVICE_HOME" || ! -d "$SERVICE_HOME" ]]; then
    fail "Could not resolve home directory for '$SERVICE_USER'."
    exit 1
fi

# ── 2. SSH key authorization ─────────────────────────────────────────────────
step 2 "Authorizing PatchPilot SSH public key"
SSH_DIR="$SERVICE_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SSH_DIR"
if [[ ! -f "$AUTH_KEYS" ]]; then
    install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$AUTH_KEYS"
fi

if grep -qxF "$PUBLIC_KEY" "$AUTH_KEYS" 2>/dev/null; then
    ok "Public key already authorized"
else
    printf "%s\n" "$PUBLIC_KEY" >> "$AUTH_KEYS"
    chown "$SERVICE_USER":"$SERVICE_USER" "$AUTH_KEYS"
    chmod 0600 "$AUTH_KEYS"
    ok "Public key appended to $AUTH_KEYS"
fi

# SELinux: restore contexts on the .ssh dir so sshd can read the keys.
# `restorecon` is a no-op on systems without SELinux.
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$SSH_DIR" 2>/dev/null || true
fi

# ── 3. Sudoers fragment ──────────────────────────────────────────────────────
step 3 "Writing /etc/sudoers.d/patchpilot"
SUDOERS_FILE="/etc/sudoers.d/patchpilot"
SUDOERS_TMP="$(mktemp)"

# Resolve binary paths, falling back to the conventional location if not
# installed yet (sudoers can list a path that doesn't exist; the entry is just
# inert until the binary appears).
_resolve() { command -v "$1" 2>/dev/null || echo "$2"; }
REBOOT_BIN="$(_resolve reboot /sbin/reboot)"
SHUTDOWN_BIN="$(_resolve shutdown /sbin/shutdown)"
SYSTEMCTL_BIN="$(_resolve systemctl /usr/bin/systemctl)"
# Ansible's become wraps every shell:/command: task in `sudo -n /bin/sh -c '...'`
# (and the apt/dnf modules in `sudo -n /usr/bin/python3 ...`). Sudo matches by
# the argv[0] it's invoked with — NOT the inner command — so a list scoped to
# /usr/bin/apt-get etc. never matches what Ansible actually runs. We allow the
# shells explicitly. Effective blast radius is unchanged: any holder of the
# PatchPilot SSH key could already gain root via `sudo apt-get install <evil>`.
SH_BIN="$(_resolve sh /bin/sh)"
BASH_BIN="$(_resolve bash /bin/bash)"
SHELL_CMDS="${SH_BIN}, ${BASH_BIN}"

case "$DISTRO_FAMILY" in
    debian)
        APT_GET_BIN="$(_resolve apt-get /usr/bin/apt-get)"
        APT_BIN="$(_resolve apt /usr/bin/apt)"
        DPKG_BIN="$(_resolve dpkg /usr/bin/dpkg)"
        UNATTENDED_BIN="$(_resolve unattended-upgrade /usr/bin/unattended-upgrade)"
        NEEDRESTART_BIN="$(_resolve needrestart /usr/sbin/needrestart)"
        SUDO_CMDS="${APT_GET_BIN}, ${APT_BIN}, ${DPKG_BIN}, ${UNATTENDED_BIN}, ${NEEDRESTART_BIN}, ${REBOOT_BIN}, ${SHUTDOWN_BIN}, ${SYSTEMCTL_BIN}, ${SHELL_CMDS}"
        ;;
    rhel)
        DNF_BIN="$(_resolve dnf /usr/bin/dnf)"
        YUM_BIN="$(_resolve yum /usr/bin/yum)"
        NEEDS_RESTARTING_BIN="$(_resolve needs-restarting /usr/bin/needs-restarting)"
        SUDO_CMDS="${DNF_BIN}, ${YUM_BIN}, ${NEEDS_RESTARTING_BIN}, ${REBOOT_BIN}, ${SHUTDOWN_BIN}, ${SYSTEMCTL_BIN}, ${SHELL_CMDS}"
        ;;
    suse)
        ZYPPER_BIN="$(_resolve zypper /usr/bin/zypper)"
        SUDO_CMDS="${ZYPPER_BIN}, ${REBOOT_BIN}, ${SHUTDOWN_BIN}, ${SYSTEMCTL_BIN}, ${SHELL_CMDS}"
        ;;
    arch)
        PACMAN_BIN="$(_resolve pacman /usr/bin/pacman)"
        SUDO_CMDS="${PACMAN_BIN}, ${REBOOT_BIN}, ${SHUTDOWN_BIN}, ${SYSTEMCTL_BIN}, ${SHELL_CMDS}"
        ;;
esac

# SETENV: applies to every command that follows on the line. Required because
# Ansible become uses sudo -E and apt/dnf set their own env vars (e.g.
# DEBIAN_FRONTEND=noninteractive); without it sudo refuses with
# "sorry, you are not allowed to set the following environment variables".
cat > "$SUDOERS_TMP" <<EOF
# /etc/sudoers.d/patchpilot — generated by Enable-PatchPilotSSH.sh
# Allows headless patching by the PatchPilot SSH user without password prompts.
# DO NOT edit by hand; re-run the setup script to regenerate.

${SERVICE_USER} ALL=(ALL) NOPASSWD: SETENV: ${SUDO_CMDS}
EOF

# Validate before installing — visudo refuses to write a bad fragment, but we
# also want to avoid clobbering an existing valid file with a broken one.
if ! visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
    fail "Generated sudoers fragment failed visudo syntax check."
    cat "$SUDOERS_TMP" >&2
    rm -f "$SUDOERS_TMP"
    exit 1
fi

if [[ -f "$SUDOERS_FILE" ]] && cmp -s "$SUDOERS_TMP" "$SUDOERS_FILE"; then
    ok "Sudoers fragment already up to date"
    rm -f "$SUDOERS_TMP"
else
    if [[ -f "$SUDOERS_FILE" ]]; then
        cp "$SUDOERS_FILE" "${SUDOERS_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
        info "Existing fragment backed up to ${SUDOERS_FILE}.bak.*"
    fi
    install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
    rm -f "$SUDOERS_TMP"
    ok "Wrote $SUDOERS_FILE"
fi

# ── 4. sshd ──────────────────────────────────────────────────────────────────
step 4 "Ensuring sshd is running and enabled"
if [[ "$SKIP_SSHD" -eq 1 ]]; then
    skip "--skip-sshd set"
elif ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found — start/enable sshd manually for your init system."
else
    # Service unit name varies: ssh on Debian/Ubuntu, sshd on RHEL/SUSE/Arch.
    SSHD_UNIT=""
    for u in ssh sshd; do
        if systemctl list-unit-files --no-legend "${u}.service" 2>/dev/null | grep -q "${u}.service"; then
            SSHD_UNIT="$u"
            break
        fi
    done
    if [[ -z "$SSHD_UNIT" ]]; then
        warn "Could not find an ssh/sshd service unit — install openssh-server and re-run."
    else
        systemctl enable --now "${SSHD_UNIT}.service" >/dev/null 2>&1 && \
            ok "${SSHD_UNIT}.service enabled and running" || \
            warn "Could not enable/start ${SSHD_UNIT}.service automatically."
    fi
fi

# ── 5. Summary ───────────────────────────────────────────────────────────────
step 5 "Summary"
ok "$SERVICE_USER@$HOSTNAME_FQDN is ready for PatchPilot management."
info "Next: add this host in PatchPilot (host: $HOSTNAME_FQDN, user: $SERVICE_USER, port: 22)."
info "Migration from root SSH: the existing root authorized_keys are untouched."
info "Once you've verified PatchPilot can connect as '$SERVICE_USER', remove the host's root entry there if desired."
