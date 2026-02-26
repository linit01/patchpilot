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


# ── Restore-from-backup setup path ────────────────────────────────────────────
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from fastapi import UploadFile, File


@router.post("/restore")
async def restore_from_backup(file: UploadFile = File(...)):
    """
    First-run restore path.  Upload a PatchPilot backup archive (.tar.gz) and
    it will:
      1. Verify the archive contains a valid pg_dump and metadata
      2. Restore the database (drop + recreate + pg_restore)
      3. Restore Ansible configuration files
      4. Mark setup as complete (users already exist in the restored DB)
      5. Schedule a self-restart so all connection pools reinitialise cleanly

    Only available before setup is complete (no users exist yet).
    Returns the same shape as /api/setup/complete so the frontend can reuse
    the same done-screen.
    """
    pool = get_db_pool()

    # Gate: only allowed before setup is complete
    if await _setup_is_complete(pool):
        raise HTTPException(
            status_code=400,
            detail="Setup is already complete. Use Settings → Backup & Restore to restore.",
        )

    if not file.filename.endswith(".tar.gz"):
        raise HTTPException(400, detail="File must be a .tar.gz PatchPilot backup archive.")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, detail="Uploaded file is empty.")

    MAX_MB = int(os.getenv("MAX_BACKUP_SIZE_MB", "500"))
    if len(content) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, detail=f"Archive exceeds {MAX_MB} MB limit.")

    pg_host     = os.getenv("POSTGRES_HOST", "postgres")
    pg_port     = os.getenv("POSTGRES_PORT", "5432")
    pg_user     = os.getenv("POSTGRES_USER", "patchpilot")
    pg_password = os.getenv("POSTGRES_PASSWORD", "patchpilot")
    pg_db       = os.getenv("POSTGRES_DB", "patchpilot")
    ansible_dir = Path(os.getenv("ANSIBLE_DIR", "/ansible"))

    pg_env = {**os.environ, "PGPASSWORD": pg_password}

    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ── 1. Extract archive ─────────────────────────────────────────────
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
                tar.extractall(tmp_path)
        except Exception as e:
            raise HTTPException(400, detail=f"Could not extract archive: {e}")

        dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        if not dirs:
            raise HTTPException(400, detail="Archive appears empty or corrupt.")
        staging = dirs[0]

        # ── 2. Validate required files ─────────────────────────────────────
        dump_file = staging / "patchpilot.dump"
        if not dump_file.exists():
            raise HTTPException(400, detail="patchpilot.dump not found — invalid backup archive.")

        meta: dict = {}
        meta_file = staging / "backup_metadata.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                warnings.append("backup_metadata.json unreadable — proceeding anyway.")
        else:
            warnings.append("backup_metadata.json missing — archive may be from an older version.")

        # ── 3. Drop + recreate database ────────────────────────────────────
        import asyncpg
        try:
            admin_conn = await asyncpg.connect(
                host=pg_host, port=int(pg_port),
                user=pg_user, password=pg_password,
                database="postgres", timeout=10,
            )
            await admin_conn.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{pg_db}' AND pid <> pg_backend_pid()
            """)
            # Close app pool before dropping DB
            try:
                existing = get_db_pool()
                if existing:
                    await existing.close()
            except Exception:
                pass
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{pg_db}"')
            await admin_conn.execute(f'CREATE DATABASE "{pg_db}" OWNER "{pg_user}"')
            await admin_conn.close()
        except Exception as e:
            raise HTTPException(500, detail=f"Failed to reset database: {e}")

        # ── 4. pg_restore ──────────────────────────────────────────────────
        result = subprocess.run(
            ["pg_restore",
             "-h", pg_host, "-p", pg_port, "-U", pg_user, "-d", pg_db,
             "--no-password", "--exit-on-error", str(dump_file)],
            capture_output=True, text=True, env=pg_env, timeout=300,
        )
        if result.returncode != 0:
            errors = [l for l in result.stderr.splitlines() if "ERROR" in l]
            if errors:
                raise HTTPException(500, detail="pg_restore failed: " + "; ".join(errors[:3]))
            warnings.append("pg_restore had warnings (non-fatal).")

        # ── 5. Restore Ansible files ───────────────────────────────────────
        ansible_src = staging / "ansible"
        if ansible_src.exists() and any(ansible_src.iterdir()):
            try:
                import shutil
                # dirs_exist_ok=True merges/overwrites files without requiring
                # the destination to be absent first — avoids FileExistsError
                # when /ansible already exists from the base image or a prior run.
                shutil.copytree(ansible_src, ansible_dir, dirs_exist_ok=True)
                # Always overwrite playbook with image-bundled version —
                # it's app code, not user data.
                image_playbook = Path("/ansible-src/check-os-updates.yml")
                if image_playbook.exists():
                    shutil.copy2(image_playbook, ansible_dir / "check-os-updates.yml")
            except Exception as e:
                warnings.append(f"Ansible restore partial: {e}")
        else:
            warnings.append("No Ansible files in backup — skipped.")

        # ── 5b. Restore encryption key to .env ────────────────────────────
        enc_key_file = staging / "encryption_key.json"
        env_file_path = Path("/install/.env")
        if not env_file_path.exists() and os.getenv("INSTALL_DIR"):
            env_file_path = Path(os.getenv("INSTALL_DIR")) / ".env"

        if enc_key_file.exists():
            try:
                enc_key_data = json.loads(enc_key_file.read_text())
                backup_enc_key = enc_key_data.get("PATCHPILOT_ENCRYPTION_KEY", "")
                current_enc_key = os.getenv("PATCHPILOT_ENCRYPTION_KEY", "")
                if backup_enc_key and backup_enc_key != current_enc_key:
                    if env_file_path.exists():
                        import re as _re
                        env_text = env_file_path.read_text()
                        if "PATCHPILOT_ENCRYPTION_KEY=" in env_text:
                            env_text = _re.sub(
                                r"^PATCHPILOT_ENCRYPTION_KEY=.*$",
                                f"PATCHPILOT_ENCRYPTION_KEY={backup_enc_key}",
                                env_text, flags=_re.MULTILINE,
                            )
                        else:
                            env_text += f"\nPATCHPILOT_ENCRYPTION_KEY={backup_enc_key}\n"
                        env_file_path.write_text(env_text)
                    else:
                        hint = Path(os.getenv("BACKUP_DIR", "/backups")) / "RESTORE_ENCRYPTION_KEY.txt"
                        hint.write_text(
                            f"PATCHPILOT_ENCRYPTION_KEY={backup_enc_key}\n\n"
                            "Add this to your .env and restart the backend.\n"
                        )
                        warnings.append(
                            f"Encryption key mismatch — .env not writable. "
                            f"Key written to {hint}. Add it to .env and restart."
                        )
            except Exception as e:
                warnings.append(f"Could not restore encryption key: {e}")

        # ── 6. Schedule self-restart ───────────────────────────────────────
        restarting = False
        own_id = os.environ.get("HOSTNAME", "")
        if own_id and Path("/var/run/docker.sock").exists():
            r = subprocess.run(
                ["docker", "run", "--rm", "-d",
                 "-v", "/var/run/docker.sock:/var/run/docker.sock",
                 "docker:cli", "sh", "-c",
                 f"sleep 5 && docker restart {own_id}"],
                capture_output=True, text=True, timeout=15,
            )
            restarting = r.returncode == 0
            if not restarting:
                warnings.append(
                    "Could not schedule auto-restart. Run manually: "
                    "docker restart patchpilot-backend-1"
                )

    return {
        "status": "restored",
        "message": (
            "Restore complete — backend restarting in 5 s. "
            "The login page will be ready in ~15 seconds."
            if restarting else
            "Restore complete. Restart the backend container to finish: "
            "docker restart patchpilot-backend-1"
        ),
        "restarting": restarting,
        "source_version": meta.get("app_version", "unknown"),
        "source_date": meta.get("created_at", "unknown"),
        "warnings": warnings,
        "users_created": 0,
        "hosts_imported": 0,
        "ssh_keys_imported": 0,
    }
