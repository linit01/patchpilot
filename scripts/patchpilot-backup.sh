#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PatchPilot — Backup & Restore CLI Tool
# For manual / cron / disaster-recovery use from the Docker Compose host.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
NC='\033[0m'

# ── Config (override via env vars) ───────────────────────────────────────────
PATCHPILOT_URL="${PATCHPILOT_URL:-http://localhost:8000}"
BACKUP_API="${PATCHPILOT_URL}/api/backup"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-./backups}"
COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

# ── Helpers ──────────────────────────────────────────────────────────────────
ok()   { echo -e "${GREEN}✓${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
step() { echo; echo -e "${PURPLE}▸ $*${NC}"; echo "────────────────────────────────────────"; }

check_api() {
    if ! curl -sf "${BACKUP_API}/health" > /dev/null 2>&1; then
        err "Cannot reach PatchPilot API at ${PATCHPILOT_URL}"
        err "Is the service running? Try: ${COMPOSE_CMD} ps"
        exit 1
    fi
}

poll_progress() {
    local max_wait="${1:-300}"
    local elapsed=0
    local prev_pct=-1
    while [ $elapsed -lt $max_wait ]; do
        local status
        status=$(curl -sf "${BACKUP_API}/status" 2>/dev/null || echo '{}')
        local maint pct msg step_name
        maint=$(echo "$status" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('maintenance_mode','false'))" 2>/dev/null || echo "false")
        pct=$(echo "$status"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('progress',{}).get('percent',0))" 2>/dev/null || echo "0")
        msg=$(echo "$status"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('progress',{}).get('message',''))" 2>/dev/null || echo "")
        step_name=$(echo "$status" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('progress',{}).get('step',''))" 2>/dev/null || echo "")

        if [ "$pct" != "$prev_pct" ]; then
            printf "\r  [%-40s] %3s%%  %s" "$(printf '%.0s#' $(seq 1 $((pct * 40 / 100))))" "$pct" "$msg"
            prev_pct=$pct
        fi

        if [ "$step_name" = "complete" ] || [ "$pct" = "100" ]; then
            echo
            return 0
        fi

        if [ "$step_name" = "error" ]; then
            echo
            err "Operation failed: $msg"
            return 1
        fi

        if [ "$maint" = "False" ] && [ "$pct" = "0" ] && [ $elapsed -gt 5 ]; then
            echo
            warn "Maintenance mode ended — operation may have completed or failed. Check API."
            return 0
        fi

        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo
    warn "Timed out waiting for operation to complete"
    return 1
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_backup() {
    local description="${1:-Manual CLI backup}"
    local include_key="${2:-false}"

    step "Creating PatchPilot backup"
    check_api

    info "Description: ${description}"
    info "Include encryption key: ${include_key}"

    local params="description=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$description")&include_encryption_key=${include_key}"
    local result
    result=$(curl -sf -X POST "${BACKUP_API}/create?${params}" 2>&1)
    echo "$result" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('message',''))" 2>/dev/null || info "$result"

    info "Polling progress…"
    poll_progress 300
    ok "Backup complete!"

    # Show the created file
    sleep 2
    local list
    list=$(curl -sf "${BACKUP_API}/list" 2>/dev/null)
    local latest
    latest=$(echo "$list" | python3 -c "import sys,json; d=json.load(sys.stdin); backups=d.get('backups',[]); print(backups[0]['filename'] if backups else '')" 2>/dev/null || echo "")
    if [ -n "$latest" ]; then
        ok "Latest backup: ${latest}"
    fi
}

cmd_list() {
    step "Available backups"
    check_api
    local list
    list=$(curl -sf "${BACKUP_API}/list")
    echo "$list" | python3 3<<"PYEOF"
import sys, json
from datetime import datetime

data = json.load(sys.stdin)
backups = data.get("backups", [])
if not backups:
    print("  No backups found.")
    sys.exit(0)

print(f"  {'FILENAME':<45} {'CREATED':<22} {'SIZE':>9}  CONTENTS")
print("  " + "─" * 95)
for b in backups:
    try:
        created = datetime.fromisoformat(b["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        created = b["created_at"][:19]
    ansible = "✓ ansible" if b["includes_ansible"] else "         "
    key     = "🔑 key" if b["includes_encryption_key"] else "      "
    desc    = b.get("description","") or ""
    desc    = (desc[:20] + "…") if len(desc) > 21 else desc
    print(f"  {b['filename']:<45} {created:<22} {b['size_human']:>9}  {ansible} {key}  {desc}")

print()
print(f"  Total: {len(backups)} backup(s)  |  Retention: last {data.get('retain_count',10)}")
PYEOF
}

cmd_download() {
    local filename="${1:-}"
    if [ -z "$filename" ]; then
        err "Usage: $0 download <filename>"
        err "Run '$0 list' to see available backups"
        exit 1
    fi

    step "Downloading backup"
    check_api
    mkdir -p "$DOWNLOAD_DIR"
    local dest="${DOWNLOAD_DIR}/${filename}"
    info "Downloading ${filename} → ${dest}"
    curl -f -# -o "$dest" "${BACKUP_API}/download/${filename}"
    ok "Downloaded: ${dest} ($(du -sh "$dest" | cut -f1))"
}

cmd_restore() {
    local filename="${1:-}"
    if [ -z "$filename" ]; then
        err "Usage: $0 restore <filename>"
        exit 1
    fi

    step "⚠️  RESTORE — Destructive Operation"
    warn "This will PERMANENTLY REPLACE all current data with the backup contents."
    warn "Target: ${filename}"
    echo
    read -rp "  Type 'yes I am sure' to continue: " confirm
    if [ "$confirm" != "yes I am sure" ]; then
        info "Restore cancelled."
        exit 0
    fi

    check_api

    info "Triggering restore via API…"
    local payload="{\"filename\":\"${filename}\",\"confirm\":true}"
    local result
    result=$(curl -sf -X POST "${BACKUP_API}/restore" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>&1)
    echo "$result" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('message',''))" 2>/dev/null || warn "$result"

    info "Polling restore progress…"
    poll_progress 600
    ok "Restore complete! The application database has been replaced."
    warn "If running in k8s, consider restarting the backend pod to refresh connections:"
    warn "  kubectl rollout restart deployment/patchpilot-backend -n patchpilot"
}

cmd_upload() {
    local filepath="${1:-}"
    if [ -z "$filepath" ] || [ ! -f "$filepath" ]; then
        err "Usage: $0 upload <path-to-backup.tar.gz>"
        exit 1
    fi

    step "Uploading backup archive"
    check_api
    info "Uploading: ${filepath}"
    curl -sf -X POST "${BACKUP_API}/upload" \
        -F "file=@${filepath}" \
        | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f\"  ✓ Uploaded: {d.get('filename','')} ({d.get('size_human','')})\")"
    ok "Upload complete. Run '$0 list' to verify."
}

cmd_delete() {
    local filename="${1:-}"
    if [ -z "$filename" ]; then
        err "Usage: $0 delete <filename>"
        exit 1
    fi
    step "Deleting backup"
    check_api
    read -rp "  Delete ${filename}? [y/N]: " confirm
    [ "$confirm" = "y" ] || { info "Cancelled."; exit 0; }
    curl -sf -X DELETE "${BACKUP_API}/delete/${filename}" > /dev/null
    ok "Deleted: ${filename}"
}

cmd_health() {
    step "Backup subsystem health"
    check_api
    curl -sf "${BACKUP_API}/health" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k, v in d.items():
    print(f'  {k:<30} {v}')
"
}

cmd_help() {
    echo ""
    echo -e "${PURPLE}PatchPilot Backup & Restore CLI${NC}"
    echo ""
    echo "  Usage: $0 <command> [options]"
    echo ""
    echo "  Commands:"
    echo "    backup [description] [include_key=false]  Create a new backup"
    echo "    list                                       List available backups"
    echo "    download <filename>                        Download backup to ./backups/"
    echo "    upload   <file.tar.gz>                     Upload backup archive to server"
    echo "    restore  <filename>                        Restore from a backup (destructive)"
    echo "    delete   <filename>                        Delete a backup archive"
    echo "    health                                     Show backup subsystem health"
    echo ""
    echo "  Environment:"
    echo "    PATCHPILOT_URL   API base URL  (default: http://localhost:8000)"
    echo "    DOWNLOAD_DIR     Download dir  (default: ./backups)"
    echo ""
    echo "  Examples:"
    echo "    $0 backup 'Pre-upgrade snapshot'"
    echo "    $0 backup 'With keys' true"
    echo "    $0 list"
    echo "    $0 download patchpilot_backup_20260220_020000.tar.gz"
    echo "    $0 restore patchpilot_backup_20260220_020000.tar.gz"
    echo ""
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
case "${1:-help}" in
    backup)   cmd_backup   "${2:-Manual CLI backup}" "${3:-false}" ;;
    list)     cmd_list ;;
    download) cmd_download "${2:-}" ;;
    upload)   cmd_upload   "${2:-}" ;;
    restore)  cmd_restore  "${2:-}" ;;
    delete)   cmd_delete   "${2:-}" ;;
    health)   cmd_health ;;
    *)        cmd_help ;;
esac
