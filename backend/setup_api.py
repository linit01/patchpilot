"""
PatchPilot - First-Run Setup API
Handles the initial setup wizard for fresh installs.
All endpoints here are PUBLIC (no auth required) but are locked out
once setup is complete (i.e., at least one user exists in the DB).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
import bcrypt
import logging
import os

def _install_defaults() -> dict:
    """Return install-time defaults from environment variables set by install-k3s.sh."""
    return {
        "default_ssh_user":    os.getenv("DEFAULT_SSH_USER", "root"),
        "default_ssh_port":    int(os.getenv("DEFAULT_SSH_PORT", "22")),
        "backup_storage_type": os.getenv("BACKUP_STORAGE_TYPE", "local"),
        "nfs_server":          os.getenv("NFS_SERVER", ""),
        "nfs_share":           os.getenv("NFS_SHARE", ""),
    }

from dependencies import get_db_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])


# ============================================================================
# Pydantic Models
# ============================================================================

class AdminAccount(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=8, max_length=255)


class GeneralSettings(BaseModel):
    app_title: str = Field(default="PatchPilot", max_length=100)
    timezone: str = Field(default="UTC", max_length=100)
    site_url: str = Field(default="", max_length=500)
    refresh_interval: int = Field(default=300, ge=30, le=86400)
    default_ssh_user: str = Field(default="root", max_length=100)
    default_ssh_port: int = Field(default=22, ge=1, le=65535)


class BackupSettings(BaseModel):
    storage_type: str = Field(default="local", pattern="^(local|nfs)$")
    nfs_server: Optional[str] = Field(default="", max_length=255)
    nfs_share: Optional[str] = Field(default="", max_length=500)
    retain_count: int = Field(default=10, ge=1, le=100)


class FirstHost(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=255)
    ssh_user: str = Field(default="root", max_length=100)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    notes: Optional[str] = Field(default="", max_length=1000)


class SetupSSHKey(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    key: str = Field(..., min_length=1)


class SetupCompleteRequest(BaseModel):
    admin: AdminAccount
    settings: GeneralSettings
    backup: BackupSettings
    hosts: Optional[List[FirstHost]] = Field(default=[])
    ssh_key: Optional[SetupSSHKey] = None


# ============================================================================
# Helper
# ============================================================================

async def _setup_is_complete(pool) -> bool:
    """Returns True if at least one user already exists (setup done)."""
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            return count > 0
    except Exception:
        return False


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/status")
async def setup_status():
    """
    Check whether first-run setup is required.
    Returns { setup_required: bool }
    Called by every page on load to gate access.
    """
    pool = await get_db_pool()
    complete = await _setup_is_complete(pool)
    return {
        "setup_required": not complete,
        "has_users": complete,
        "install_defaults": _install_defaults(),
    }


@router.post("/complete")
async def complete_setup(payload: SetupCompleteRequest):
    """
    Execute the full first-run setup in a single atomic transaction:
      1. Create admin user
      2. Write all settings
      3. Optionally create first host(s)

    This endpoint returns 403 if any user already exists — there is no
    way to re-run setup through the API once it is complete.
    """
    pool = await get_db_pool()

    if await _setup_is_complete(pool):
        raise HTTPException(
            status_code=403,
            detail="Setup is already complete. Use the Settings page to make changes."
        )

    async with pool.acquire() as conn:
        async with conn.transaction():

            # ── 1. Create admin user ──────────────────────────────────────────
            password_hash = _hash_password(payload.admin.password)
            user_id = await conn.fetchval("""
                INSERT INTO users (username, email, password_hash, role, is_active)
                VALUES ($1, $2, $3, 'admin', true)
                RETURNING id
            """, payload.admin.username, payload.admin.email, password_hash)

            logger.info(f"[Setup] Admin user '{payload.admin.username}' created (id={user_id})")

            # ── 2. Write settings ─────────────────────────────────────────────
            s = payload.settings
            b = payload.backup

            settings_rows = [
                ("app_title",          payload.settings.app_title,
                 "Application display name"),
                ("schedule_timezone",  s.timezone,
                 "Timezone for scheduled patches (e.g. America/Chicago)"),
                ("app_base_url",       s.site_url,
                 "Public URL of this PatchPilot instance"),
                ("refresh_interval",   str(s.refresh_interval),
                 "Dashboard auto-refresh interval in seconds"),
                ("default_ssh_user",   s.default_ssh_user,
                 "Default SSH username for new hosts"),
                ("default_ssh_port",   str(s.default_ssh_port),
                 "Default SSH port for new hosts"),
                ("allowed_origins",    s.site_url if s.site_url else "*",
                 "CORS allowed origins (comma-separated URLs or *)"),
                # Backup settings
                ("backup_storage_type", b.storage_type,
                 "Backup storage type: local or nfs"),
                ("backup_nfs_server",  b.nfs_server or "",
                 "NFS server hostname or IP (e.g. 192.168.1.10)"),
                ("backup_nfs_share",   b.nfs_share or "",
                 "NFS share path (e.g. /mnt/backups/patchpilot)"),
                ("backup_retain_count", str(b.retain_count),
                 "Number of backup archives to keep"),
            ]

            for key, value, description in settings_rows:
                await conn.execute("""
                    INSERT INTO settings (key, value, description)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            description = EXCLUDED.description,
                            updated_at = NOW()
                """, key, value, description)

            logger.info("[Setup] Settings written")

            # ── 3. Optionally create first hosts ──────────────────────────────
            hosts_created = []
            for h in (payload.hosts or []):
                if not h.hostname.strip():
                    continue
                host_id = await conn.fetchval("""
                    INSERT INTO hosts (hostname, ssh_user, ssh_port, notes, status, total_updates, ssh_key_type)
                    VALUES ($1, $2, $3, $4, 'unknown', 0, 'default')
                    ON CONFLICT (hostname) DO NOTHING
                    RETURNING id
                """, h.hostname.strip(), h.ssh_user, h.ssh_port, h.notes or "")
                if host_id:
                    hosts_created.append(h.hostname)
                    logger.info(f"[Setup] Host '{h.hostname}' created with ssh_key_type=default")

    # Trigger ansible inventory sync in background (non-blocking)
    try:
        from sync_ansible_inventory import sync_ansible_inventory
        await sync_ansible_inventory(pool)
    except Exception as e:
        logger.warning(f"[Setup] Ansible inventory sync after setup failed (non-fatal): {e}")

    # Save default SSH key if provided (outside transaction — non-fatal if it fails)
    ssh_key_saved = False
    if payload.ssh_key and payload.ssh_key.key.strip():
        try:
            from encryption_utils import encrypt_credential
            async with pool.acquire() as conn:
                encrypted = encrypt_credential(payload.ssh_key.key).encode('utf-8')
                await conn.execute("""
                    INSERT INTO saved_ssh_keys (name, ssh_key_encrypted, is_default)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (name) DO UPDATE
                        SET ssh_key_encrypted = EXCLUDED.ssh_key_encrypted,
                            is_default = TRUE,
                            updated_at = NOW()
                """, payload.ssh_key.name, encrypted)
                ssh_key_saved = True
                logger.info(f"[Setup] Default SSH key '{payload.ssh_key.name}' saved")
        except Exception as e:
            logger.warning(f"[Setup] SSH key save failed (non-fatal): {e}")

    return {
        "success": True,
        "message": "Setup complete! Redirecting to login...",
        "admin_username": payload.admin.username,
        "hosts_created": hosts_created,
        "backup_type": payload.backup.storage_type,
        "ssh_key_saved": ssh_key_saved,
    }


@router.get("/backup-config-hint")
async def backup_config_hint():
    """
    Return deployment-specific instructions for NFS backup configuration.
    Used by the setup wizard to show the user what to add to their
    docker-compose.yml or k8s PVC after choosing NFS.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        nfs_server = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'backup_nfs_server'"
        ) or ""
        nfs_share = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'backup_nfs_share'"
        ) or ""
        storage_type = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'backup_storage_type'"
        ) or "local"

    docker_snippet = ""
    k8s_snippet = ""

    if storage_type == "nfs" and nfs_server and nfs_share:
        docker_snippet = f"""  backups:
    driver: local
    driver_opts:
      type: nfs
      o: addr={nfs_server},rw,nfsvers=4
      device: ":{nfs_share}" """

        k8s_snippet = f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: patchpilot-backups-pv
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  nfs:
    server: {nfs_server}
    path: {nfs_share}"""

    return {
        "storage_type": storage_type,
        "nfs_server": nfs_server,
        "nfs_share": nfs_share,
        "docker_compose_snippet": docker_snippet,
        "k8s_pv_snippet": k8s_snippet,
    }
