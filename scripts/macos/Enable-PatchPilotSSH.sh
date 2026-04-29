#!/usr/bin/env bash
#
# Enable-PatchPilotSSH.sh — prepare a macOS host for PatchPilot management.
#
# Mirrors the Windows companion script (scripts/windows/Enable-PatchPilotSSH.ps1)
# for the macOS path:
#
#   1. Enables Remote Login (sshd)
#   2. Authorizes a PatchPilot SSH public key in ~/.ssh/authorized_keys
#   3. Writes /etc/sudoers.d/patchpilot with the canonical SETENV: NOPASSWD
#      entries so headless patch runs do not prompt for a password
#   4. Installs GNU coreutils via Homebrew (for the gtimeout binary used by
#      the App Store update task — soft warning only if missing)
#   5. Detects mas; prints install instructions if not present
#
# The sudoers fragment uses SETENV: because Ansible become passes env vars
# (and mas internally sets MAS_NO_AUTO_INDEX). Without SETENV: sudo refuses
# with: "sorry, you are not allowed to set the following environment variables".
#
# Run this script as the operator's own user account on each Mac:
#
#   curl -fsSL https://<patchpilot-host>/scripts/macos/Enable-PatchPilotSSH.sh | \
#     bash -s -- --public-key "ssh-ed25519 AAAA... patchpilot"
#
# Or download and run locally:
#
#   ./Enable-PatchPilotSSH.sh --public-key "ssh-ed25519 AAAA... patchpilot"
#
# Requires: macOS 12+, Homebrew, sudo privileges (you will be prompted once
# for your sudo password during initial setup).

set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────────────
PUBLIC_KEY=""
SKIP_COREUTILS=0
SKIP_REMOTE_LOGIN=0

usage() {
    cat <<USAGE
Usage: $0 --public-key "ssh-ed25519 AAAA... patchpilot" [options]

Options:
  --public-key KEY        SSH public key to authorize (required)
  --skip-coreutils        Skip 'brew install coreutils'
  --skip-remote-login     Skip 'systemsetup -setremotelogin on'
  -h, --help              Show this help

USAGE
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --public-key)        PUBLIC_KEY="$2"; shift 2 ;;
        --skip-coreutils)    SKIP_COREUTILS=1; shift ;;
        --skip-remote-login) SKIP_REMOTE_LOGIN=1; shift ;;
        -h|--help)           usage 0 ;;
        *)                   echo "Unknown arg: $1" >&2; usage 1 ;;
    esac
done

# ── Colors / helpers ─────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
    C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_RESET=""
fi

TOTAL_STEPS=6
step() { printf "\n%s[%s/%s] %s%s\n" "$C_CYAN" "$1" "$TOTAL_STEPS" "$2" "$C_RESET"; }
ok()   { printf "    %sOK:%s %s\n"   "$C_GREEN"  "$C_RESET" "$1"; }
skip() { printf "    %sSKIP:%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
warn() { printf "    %sWARN:%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
fail() { printf "    %sFAIL:%s %s\n" "$C_RED"    "$C_RESET" "$1" >&2; }
info() { printf "    %s%s%s\n"       "$C_DIM"    "$1" "$C_RESET"; }

if [[ "$(uname)" != "Darwin" ]]; then
    fail "This script is for macOS. Detected: $(uname)."
    exit 1
fi

if [[ "$EUID" -eq 0 ]]; then
    fail "Do not run this script with sudo. Run as your own user; sudo will be invoked when needed."
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

SSH_USER="$(id -un)"
ARCH="$(uname -m)"
case "$ARCH" in
    arm64)  BREW_PREFIX="/opt/homebrew" ;;
    x86_64) BREW_PREFIX="/usr/local"    ;;
    *)      fail "Unsupported architecture: $ARCH"; exit 1 ;;
esac
MAS_BIN="${BREW_PREFIX}/bin/mas"

printf "\n%sPatchPilot macOS host setup%s\n" "$C_CYAN" "$C_RESET"
info "User:        $SSH_USER"
info "Arch:        $ARCH ($BREW_PREFIX)"
info "Sudoers:     /etc/sudoers.d/patchpilot"
info "Authorized:  ~/.ssh/authorized_keys"

# Single sudo prompt up front so the rest of the script runs without re-prompting
sudo -v

# ── 1. Remote Login (sshd) ───────────────────────────────────────────────────
step 1 "Enabling Remote Login (sshd)"
if [[ "$SKIP_REMOTE_LOGIN" -eq 1 ]]; then
    skip "--skip-remote-login set"
else
    if sudo systemsetup -getremotelogin 2>/dev/null | grep -qi "on"; then
        ok "Remote Login already enabled"
    else
        if sudo systemsetup -setremotelogin on 2>/dev/null; then
            ok "Remote Login enabled"
        else
            warn "Could not enable Remote Login non-interactively."
            info "Enable manually: System Settings → General → Sharing → Remote Login"
            info "(Newer macOS requires Full Disk Access for Terminal to toggle this via CLI.)"
        fi
    fi
fi

# ── 2. SSH key authorization ─────────────────────────────────────────────────
step 2 "Authorizing PatchPilot SSH public key"
SSH_DIR="$HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
touch "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

if grep -qxF "$PUBLIC_KEY" "$AUTH_KEYS" 2>/dev/null; then
    ok "Public key already authorized"
else
    printf "%s\n" "$PUBLIC_KEY" >> "$AUTH_KEYS"
    ok "Public key appended to $AUTH_KEYS"
fi

# ── 3. Sudoers fragment ──────────────────────────────────────────────────────
step 3 "Writing /etc/sudoers.d/patchpilot"
SUDOERS_FILE="/etc/sudoers.d/patchpilot"
SUDOERS_TMP="$(mktemp)"

# SETENV: applies to every command that follows on the line. Required because
# Ansible become uses sudo -E and tools like mas/softwareupdate set their own
# env vars (e.g. MAS_NO_AUTO_INDEX); without it sudo refuses with
# "sorry, you are not allowed to set the following environment variables".
cat > "$SUDOERS_TMP" <<EOF
# /etc/sudoers.d/patchpilot — generated by Enable-PatchPilotSSH.sh
# Allows headless patching by the PatchPilot SSH user without password prompts.
# DO NOT edit by hand; re-run the setup script to regenerate.

${SSH_USER} ALL=(ALL) NOPASSWD: SETENV: ${MAS_BIN}, /usr/sbin/softwareupdate, /sbin/reboot, /sbin/shutdown
EOF

# Validate before installing — visudo refuses to write a bad fragment, but we
# also want to avoid clobbering an existing valid file with a broken one.
if ! sudo visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
    fail "Generated sudoers fragment failed visudo syntax check."
    cat "$SUDOERS_TMP" >&2
    rm -f "$SUDOERS_TMP"
    exit 1
fi

if [[ -f "$SUDOERS_FILE" ]] && sudo cmp -s "$SUDOERS_TMP" "$SUDOERS_FILE"; then
    ok "Sudoers fragment already up to date"
    rm -f "$SUDOERS_TMP"
else
    if [[ -f "$SUDOERS_FILE" ]]; then
        sudo cp "$SUDOERS_FILE" "${SUDOERS_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
        info "Existing fragment backed up to ${SUDOERS_FILE}.bak.*"
    fi
    sudo install -m 0440 -o root -g wheel "$SUDOERS_TMP" "$SUDOERS_FILE"
    rm -f "$SUDOERS_TMP"
    ok "Wrote $SUDOERS_FILE"
fi

# ── 4. coreutils (gtimeout) ──────────────────────────────────────────────────
step 4 "Installing GNU coreutils (for gtimeout)"
if [[ "$SKIP_COREUTILS" -eq 1 ]]; then
    skip "--skip-coreutils set"
elif ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found — install from https://brew.sh and re-run."
elif command -v gtimeout >/dev/null 2>&1 || command -v timeout >/dev/null 2>&1; then
    ok "timeout/gtimeout already available"
else
    info "Running: brew install coreutils"
    if brew install coreutils >/dev/null 2>&1; then
        ok "coreutils installed"
    else
        warn "brew install coreutils failed — mas updates will run without per-app hard timeout (still bounded by Ansible async)."
    fi
fi

# ── 5. mas detection ─────────────────────────────────────────────────────────
step 5 "Checking mas (App Store CLI)"
if [[ -x "$MAS_BIN" ]]; then
    ok "mas found at $MAS_BIN"
    info "If automated App Store updates are enabled in PatchPilot, ensure you are signed into the App Store interactively at least once on this host."
else
    warn "mas not installed at $MAS_BIN"
    info "Install with: brew install mas"
    info "(Optional — only needed if you want PatchPilot to apply App Store updates.)"
fi

# ── 6. Summary ───────────────────────────────────────────────────────────────
step 6 "Summary"
ok "$SSH_USER@$(hostname) is ready for PatchPilot management."
info "Next: add this host in PatchPilot (host: $(hostname), user: $SSH_USER, port: 22)."
info "Manual interactive sudo (e.g. 'brew upgrade' for casks needing /Applications) still prompts for your password — the sudoers fragment only covers PatchPilot's automated path."
