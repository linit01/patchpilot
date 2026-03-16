#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — Repo Sanitization Script
# Run this from the repo root BEFORE running git filter-repo.
#
# What this does:
#   1. Fixes current files (replaces personal data with generic examples)
#   2. Renames .env.bak → .env.example
#   3. Removes files that shouldn't be public
#   4. Updates README/QUICKSTART version badges and URLs
#   5. Prints git filter-repo commands to run afterward
#
# Usage:
#   cd ~/github/patchpilot
#   chmod +x scripts/sanitize-for-public.sh
#   ./scripts/sanitize-for-public.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }

# Cross-platform sed -i
sed_i() { sed -i '' "$@" 2>/dev/null || sed -i "$@"; }

echo ""
echo -e "${BLUE}PatchPilot — Repo Sanitization${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Verify we're in the repo root
[[ -f "VERSION" && -f "docker-compose.yml" ]] || {
    echo -e "${RED}✗ Run this from the patchpilot repo root${NC}"
    exit 1
}

VERSION="$(cat VERSION | tr -d '[:space:]')"
info "Current version: ${VERSION}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. README.md — remove personal email, fix version badge
# ─────────────────────────────────────────────────────────────────────────────
info "Sanitizing README.md..."

# Replace the personal header block
sed_i 's/^# Designed, built and owned by John R\. Sanborn, 2026\.$/# PatchPilot — Patch Management for Linux \& macOS/' README.md
sed_i '/^# Code and design advice by Claude\.AI/d' README.md
sed_i '/^# UI designs inspired by PiHole/d' README.md
sed_i '/^# contact@getpatchpilot.app$/d' README.md
sed_i '/^# Git repo: linit01\/patchpilot$/d' README.md
sed_i '/^# Docker hub: linit01\/patchpilot$/d' README.md
sed_i 's/^# All rights to this code and design ideas are reserved by owner\./# https:\/\/github.com\/linit01\/patchpilot/' README.md

# Fix version badge
sed_i "s/version-0\.10\.0--alpha/version-${VERSION//./%2E}/" README.md

ok "README.md"

# ─────────────────────────────────────────────────────────────────────────────
# 2. KUBERNETES.md — replace example.com, sanborn username
# ─────────────────────────────────────────────────────────────────────────────
info "Sanitizing KUBERNETES.md..."

sed_i 's/sanbornhome\.com/example.com/g' KUBERNETES.md
sed_i 's/patchpilot\.sanbornhome\.com/patchpilot.example.com/g' KUBERNETES.md
sed_i 's/you@sanbornhome\.com/you@example.com/g' KUBERNETES.md
sed_i 's/defaultSshUser: root/defaultSshUser: root/g' KUBERNETES.md

ok "KUBERNETES.md"

# ─────────────────────────────────────────────────────────────────────────────
# 3. QUICKSTART.md — fix placeholder repo URL
# ─────────────────────────────────────────────────────────────────────────────
info "Sanitizing QUICKSTART.md..."

sed_i 's|https://github.com/yourusername/patchpilot.git|https://github.com/linit01/patchpilot.git|g' QUICKSTART.md

ok "QUICKSTART.md"

# ─────────────────────────────────────────────────────────────────────────────
# 4. k8s/install-config.yaml — replace with generic defaults
# ─────────────────────────────────────────────────────────────────────────────
info "Resetting k8s/install-config.yaml to example defaults..."

# Overwrite with the example config (which has no personal data)
cp k8s/install-config.yaml.example k8s/install-config.yaml

ok "k8s/install-config.yaml"

# ─────────────────────────────────────────────────────────────────────────────
# 5. certs/ — remove generated certs, keep only config templates
# ─────────────────────────────────────────────────────────────────────────────
info "Cleaning certs/ directory..."

rm -f certs/patchpilot-ca.crt
rm -f certs/patchpilot-ca.srl
rm -f certs/patchpilot.crt
rm -f certs/patchpilot.csr

# Fix IPs in config templates
sed_i 's/IP\.1  = 10\.0\.1\.58/IP.1  = 192.168.1.100/' certs/san.cnf
sed_i 's/IP\.1  = 192\.168\.1\.x/IP.1  = 192.168.1.100/' certs/patchpilot-openssl.cnf

ok "certs/ (removed generated certs, fixed example IPs)"

# ─────────────────────────────────────────────────────────────────────────────
# 6. backend/app.py — replace example IP in comment
# ─────────────────────────────────────────────────────────────────────────────
info "Sanitizing backend/app.py..."

sed_i 's/10\.0\.1\.106/192.168.1.50/g' backend/app.py

ok "backend/app.py"

# ─────────────────────────────────────────────────────────────────────────────
# 8. Rename .env.bak → .env.example
# ─────────────────────────────────────────────────────────────────────────────
info "Renaming .env.bak → .env.example..."

if [[ -f ".env.bak" ]]; then
    mv .env.bak .env.example
    ok ".env.example created"
else
    warn ".env.bak not found — check if .env.example already exists"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 9. Remove files that shouldn't be public
# ─────────────────────────────────────────────────────────────────────────────
info "Removing files not needed in public repo..."

rm -f NOTES
rm -f install_dependencies.sh
rm -f install.html.installer
rm -f scripts/push_new_build.sh
rm -f scripts/push_new_build.sh.old
rm -f CHANGELOG-v0.9.7a.md
rm -f k8s/install-k3s.sh.orig
rm -f k8s/install-k3s.sh.rej

ok "Removed: NOTES, install_dependencies.sh, install.html.installer, push_new_build.sh, *.old, *.orig, *.rej"

# ─────────────────────────────────────────────────────────────────────────────
# 10. Ensure .gitignore covers sensitive files
# ─────────────────────────────────────────────────────────────────────────────
info "Checking .gitignore..."

GITIGNORE_ADDITIONS=(
    ".env"
    "!.env.example"
    "scripts/push_new_build.sh"
    "certs/patchpilot-ca.crt"
    "certs/patchpilot-ca.srl"
    "certs/patchpilot.crt"
    "certs/patchpilot.csr"
    "certs/*.key"
    "patchpilot.tgz"
    "patchpilot.tgz.b64"
    "k8s/.generated/"
)

touch .gitignore
for entry in "${GITIGNORE_ADDITIONS[@]}"; do
    if ! grep -qF "$entry" .gitignore 2>/dev/null; then
        echo "$entry" >> .gitignore
        ok "Added to .gitignore: $entry"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}File sanitization complete.${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "${YELLOW}NEXT STEPS — Git history rewrite:${NC}"
echo ""
echo "  1. Install git-filter-repo (if not already):"
echo "     brew install git-filter-repo    # macOS"
echo "     pip3 install git-filter-repo    # or via pip"
echo ""
echo "  2. Commit these sanitization changes first:"
echo "     git add -A"
echo "     git commit -m 'chore: sanitize repo for public release'"
echo ""
echo "  3. Run git filter-repo to scrub history:"
echo ""
echo "     git filter-repo \\"
echo "       --replace-text <(cat <<'REPLACEMENTS'"
echo "example.com==>example.com"
echo "contact@getpatchpilot.app==>contact@getpatchpilot.app"
echo "contact@getpatchpilot.app==>contact@getpatchpilot.app"
echo "192.168.1.100==>192.168.1.100"
echo "192.168.1.50==>192.168.1.50"
echo "192.168.1.100==>192.168.1.100"
echo "$(dirname "$0")/..==>\$(dirname \"\$0\")/.."
echo "defaultSshUser: root==>defaultSshUser: root"
echo "REPLACEMENTS"
echo "     ) \\"
echo "       --path-glob 'certs/patchpilot-ca.crt' --invert-paths \\"
echo "       --path-glob 'certs/patchpilot-ca.srl' --invert-paths \\"
echo "       --path-glob 'certs/patchpilot.crt' --invert-paths \\"
echo "       --path-glob 'certs/patchpilot.csr' --invert-paths \\"
echo "       --path-glob 'certs/*.key' --invert-paths \\"
echo "       --path 'NOTES' --invert-paths \\"
echo "       --path 'install_dependencies.sh' --invert-paths \\"
echo "       --path 'install.html.installer' --invert-paths \\"
echo "       --path 'scripts/push_new_build.sh' --invert-paths \\"
echo "       --path 'scripts/push_new_build.sh.old' --invert-paths"
echo ""
echo "  4. Re-add the remote and force-push:"
echo "     git remote add origin https://github.com/linit01/patchpilot.git"
echo "     git push --force --all"
echo "     git push --force --tags"
echo ""
echo "  5. Flip the repo to public in GitHub Settings"
echo ""
echo -e "${YELLOW}⚠  git filter-repo removes the remote. Step 4 re-adds it.${NC}"
echo -e "${YELLOW}⚠  Force-push rewrites history for all collaborators.${NC}"
echo -e "${YELLOW}⚠  Make sure you have a local backup before running filter-repo.${NC}"
echo ""
