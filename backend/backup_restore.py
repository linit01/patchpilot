"""
PatchPilot - Backup & Restore Module
=====================================
Handles full application backup and restore including:
  - PostgreSQL database dump/restore
  - Application settings (settings table)
  - Ansible inventory and playbook files
  - Encryption key export (optional, user-controlled)

Backup Strategy:
  - Maintenance mode is set (rejects new patch jobs and writes)
  - All active DB connections are terminated via pg_terminate_backend
  - pg_dump runs against a quiescent database (transactionally safe)
  - A .tgz bundle is created and stored in /backups volume

Restore Strategy:
  - Maintenance mode is set
  - Connection pool is closed
  - Existing DB connections are terminated
  - dropdb / createdb clears state
  - pg_restore populates fresh DB
  - Connection pool is re-initialized
  - Maintenance mode is cleared

Docker Compose variant: optionally mounts docker.sock so the
postgres container can be hard-stopped/started for extra safety.
"""

import asyncio
import io
import json
import logging
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

ANSIBLE_DIR = Path(os.getenv("ANSIBLE_DIR", "/ansible"))

# Absolute path to the install directory on the host (contains .env).
# Set via INSTALL_DIR in .env — used to bundle .env into uninstall backups.
INSTALL_DIR = Path(os.getenv("INSTALL_DIR", "")).expanduser() if os.getenv("INSTALL_DIR") else None

PG_HOST = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_USER = os.getenv("POSTGRES_USER", "patchpilot")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "patchpilot")
PG_DB = os.getenv("POSTGRES_DB", "patchpilot")
PG_URL = os.getenv("DATABASE_URL", f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}")

BACKUP_RETAIN_COUNT = int(os.getenv("BACKUP_RETAIN_COUNT", "10"))  # Keep last N backups (env fallback)
MAX_BACKUP_SIZE_MB = int(os.getenv("MAX_BACKUP_SIZE_MB", "500"))


async def _get_retain_count() -> int:
    """Read backup_retain_count from the settings table (user-configurable via UI).
    Falls back to the BACKUP_RETAIN_COUNT env var / default."""
    if _db_pool:
        try:
            row = await _db_pool.fetchval(
                "SELECT value FROM settings WHERE key = 'backup_retain_count'"
            )
            if row is not None:
                val = int(row)
                if val >= 1:
                    return val
        except Exception:
            pass
    return BACKUP_RETAIN_COUNT

# Backup filename prefix.  Produces: patchpilot_20260306_185509.tar.gz
# Old files from earlier versions are still recognized via _BACKUP_PREFIXES.
_BACKUP_PREFIX = "patchpilot_"
_BACKUP_PREFIXES = ("patchpilot_", "patchpilot_backup_", "pp_bak_")  # current + legacy


def _is_backup_file(name: str) -> bool:
    """Return True if *name* looks like a PatchPilot backup archive."""
    return (any(name.startswith(p) for p in _BACKUP_PREFIXES)
            and (name.endswith(".tgz") or name.endswith(".tar.gz")))


def _list_backup_archives(newest_first: bool = True) -> list:
    """Return a sorted list of Path objects for all backup archives in BACKUP_DIR."""
    archives = [p for p in BACKUP_DIR.iterdir()
                if p.is_file() and _is_backup_file(p.name)]
    archives.sort(key=lambda p: p.stat().st_mtime, reverse=newest_first)
    return archives

# Docker container names (used for hard stop/start if docker.sock is mounted)
POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER_NAME", "patchpilot-postgres-1")

# ---------------------------------------------------------------------------
# State shared with main app.py
# ---------------------------------------------------------------------------
# app.py should import and use this flag to gate write operations
maintenance_mode: bool = False
maintenance_reason: str = ""
current_operation: Optional[str] = None  # "backup" | "restore" | None
operation_progress: dict = {}

# Connection pool reference — set by app.py on startup via set_pool()
_db_pool: Optional[asyncpg.Pool] = None

# DatabaseClient reference — set by app.py on startup via set_db_client()
# Used to close/reconnect the legacy db.pool after a restore so it doesn't
# hold stale connections to the dropped-and-recreated database.
_db_client = None

# Post-restore callback — set by app.py so the restore routine can trigger
# an immediate Ansible check without circular imports.
_post_restore_callback = None


def set_pool(pool: asyncpg.Pool):
    """Called from app.py after pool creation so backup module can manage it."""
    global _db_pool
    _db_pool = pool


def set_db_client(db_client):
    """Called from app.py so the backup module can rebuild the DatabaseClient
    pool after a restore (it has its own separate asyncpg pool that also needs
    reconnecting when the database is dropped and recreated)."""
    global _db_client
    _db_client = db_client


def set_post_restore_callback(callback):
    """Called from app.py to provide a coroutine that triggers an Ansible check.
    Invoked after a successful restore when no self-restart occurs."""
    global _post_restore_callback
    _post_restore_callback = callback


def get_pool() -> asyncpg.Pool:
    if _db_pool is None:
        raise RuntimeError("Database pool not initialized")
    return _db_pool


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class BackupMetadata(BaseModel):
    filename: str
    created_at: str
    size_bytes: int
    size_human: str
    postgres_version: str
    app_version: str
    includes_ansible: bool
    includes_encryption_key: bool
    description: str


class RestoreRequest(BaseModel):
    filename: str
    confirm: bool = False


class BackupListResponse(BaseModel):
    backups: list[BackupMetadata]
    backup_dir: str
    retain_count: int


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _pg_env() -> dict:
    """Return env dict with PGPASSWORD set for subprocess calls."""
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD
    return env


def _set_progress(step: str, percent: int, message: str):
    global operation_progress
    operation_progress = {"step": step, "percent": percent, "message": message}
    logger.info(f"[backup] {step} ({percent}%): {message}")


def _check_pg_client_tools():
    """Ensure pg_dump and pg_restore are available."""
    for tool in ("pg_dump", "pg_restore", "dropdb", "createdb", "psql"):
        result = subprocess.run(["which", tool], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"PostgreSQL client tool '{tool}' not found. "
                "Ensure postgresql-client is installed in the backend container."
            )


def _get_pg_version() -> str:
    try:
        result = subprocess.run(
            ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
             "-tAc", "SELECT version();"],
            capture_output=True, text=True, env=_pg_env(), timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0][:80]
    except Exception:
        pass
    return "unknown"


def _get_app_version() -> str:
    try:
        # Try to read version from a VERSION file or settings table
        version_file = Path("/app/VERSION")
        if version_file.exists():
            return version_file.read_text().strip()
    except Exception:
        pass
    return "2.0.0"


# ---------------------------------------------------------------------------
# Maintenance mode helpers
# ---------------------------------------------------------------------------
async def _enter_maintenance(reason: str):
    global maintenance_mode, maintenance_reason, current_operation
    maintenance_mode = True
    maintenance_reason = reason
    logger.info(f"Entering maintenance mode: {reason}")

    # Terminate all OTHER connections to the database so pg_dump/restore
    # has exclusive access. We keep our own connection open to manage state.
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = $1
                  AND pid <> pg_backend_pid()
                  AND state != 'idle'
            """, PG_DB)
            logger.info("Terminated active database connections")
    except Exception as e:
        logger.warning(f"Could not terminate connections (non-fatal): {e}")


async def _exit_maintenance():
    global maintenance_mode, maintenance_reason, current_operation, operation_progress
    maintenance_mode = False
    maintenance_reason = ""
    current_operation = None
    operation_progress = {}
    logger.info("Exiting maintenance mode")


# ---------------------------------------------------------------------------
# Docker hard-stop helper (optional — requires docker.sock mount)
# ---------------------------------------------------------------------------
def _docker_available() -> bool:
    return Path("/var/run/docker.sock").exists()


def _docker_stop_postgres():
    """Hard-stop the postgres container via docker CLI."""
    if not _docker_available():
        logger.warning("docker.sock not mounted; skipping hard postgres stop")
        return False
    result = subprocess.run(
        ["docker", "stop", POSTGRES_CONTAINER],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        logger.info(f"Stopped container: {POSTGRES_CONTAINER}")
        return True
    logger.warning(f"docker stop failed: {result.stderr}")
    return False


def _docker_start_postgres():
    """Restart the postgres container after restore."""
    if not _docker_available():
        return False
    result = subprocess.run(
        ["docker", "start", POSTGRES_CONTAINER],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        logger.info(f"Started container: {POSTGRES_CONTAINER}")
        return True
    logger.warning(f"docker start failed: {result.stderr}")
    return False


async def _wait_for_postgres(timeout: int = 60):
    """Poll until postgres accepts connections."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            conn = await asyncpg.connect(
                host=PG_HOST, port=int(PG_PORT),
                user=PG_USER, password=PG_PASSWORD,
                database=PG_DB, timeout=3
            )
            await conn.close()
            logger.info("PostgreSQL is accepting connections")
            return
        except Exception:
            await asyncio.sleep(2)
    raise TimeoutError("PostgreSQL did not become ready in time")


# ---------------------------------------------------------------------------
# Connection pool rebuild after restore
# ---------------------------------------------------------------------------
async def _rebuild_pool():
    """Close and recreate both connection pools after restore.

    Two pools exist:
      _db_pool   — used by settings API, auth, backup module (asyncpg.Pool)
      _db_client — DatabaseClient used by host/package/patch-history routes

    Both are connected to the database that was just dropped and recreated.
    asyncpg won't proactively reconnect invalidated pool connections; the first
    request to use a stale connection would throw an error.  Explicitly closing
    and recreating both pools gives callers a clean slate immediately.
    """
    global _db_pool, _db_client

    # ── Rebuild _db_pool (settings / auth / backup module) ────────────────
    logger.info("Rebuilding asyncpg connection pool (_db_pool)...")
    try:
        if _db_pool:
            await _db_pool.close()
    except Exception as e:
        logger.warning(f"Error closing _db_pool: {e}")

    _db_pool = await asyncpg.create_pool(
        host=PG_HOST,
        port=int(PG_PORT),
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_DB,
        min_size=1,
        max_size=10,
    )
    logger.info("_db_pool rebuilt successfully")

    # ── Sync the dependencies module so Depends(get_db_pool) uses the new pool
    from dependencies import set_pool as _deps_set_pool
    _deps_set_pool(_db_pool)
    logger.info("dependencies._pool synced with rebuilt _db_pool")

    # ── Rebuild DatabaseClient pool (host / package / patch-history) ──────
    if _db_client is not None:
        logger.info("Rebuilding DatabaseClient pool (_db_client.pool)...")
        try:
            if getattr(_db_client, 'pool', None):
                await _db_client.pool.close()
                _db_client.pool = None
        except Exception as e:
            logger.warning(f"Error closing DatabaseClient pool: {e}")

        # Re-use the client's own connect() method so it applies its own params
        try:
            await _db_client.connect()
            logger.info("DatabaseClient pool rebuilt successfully")
        except Exception as e:
            logger.error(f"Failed to rebuild DatabaseClient pool: {e}")
    else:
        logger.warning(
            "DatabaseClient reference not set — call set_db_client(db) in "
            "startup_event(). Host/package routes may get connection errors "
            "until the backend container is restarted."
        )


# ---------------------------------------------------------------------------
# BACKUP
# ---------------------------------------------------------------------------
async def _run_backup(description: str, include_encryption_key: bool,
                      uninstall_mode: bool = False) -> str:
    """
    Core backup routine. Returns the filename of the created backup archive.
    Caller is responsible for entering/exiting maintenance mode.

    uninstall_mode=True:
      - Forces include_encryption_key=True (non-negotiable for a restore to work)
      - Bundles the .env file from INSTALL_DIR if configured
      - Tags the archive filename with '_uninstall' for easy identification
    """
    _check_pg_client_tools()

    # Uninstall backups must always carry the encryption key — without it the
    # restored DB's SSH credentials are permanently unreadable.
    if uninstall_mode:
        include_encryption_key = True

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    short_id = secrets.token_hex(2)  # 4 hex chars
    suffix = "_uninstall" if uninstall_mode else ""
    backup_name = f"{_BACKUP_PREFIX}{timestamp}_{short_id}{suffix}"
    archive_path = BACKUP_DIR / f"{backup_name}.tgz"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        backup_staging = tmp_path / backup_name
        backup_staging.mkdir()

        # ── Step 1: pg_dump ──────────────────────────────────────────────
        _set_progress("database", 15, "Running pg_dump...")
        dump_file = backup_staging / "patchpilot.dump"
        pg_dump_cmd = [
            "pg_dump",
            "-h", PG_HOST, "-p", PG_PORT,
            "-U", PG_USER, "-d", PG_DB,
            "--format=custom",          # custom format: compressed, supports selective restore
            "--no-password",
            "--verbose",
            "-f", str(dump_file),
        ]
        result = subprocess.run(
            pg_dump_cmd, capture_output=True, text=True,
            env=_pg_env(), timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed:\n{result.stderr}")
        logger.info(f"pg_dump complete: {dump_file.stat().st_size} bytes")

        # ── Step 2: Export settings as JSON (human-readable supplement) ──
        _set_progress("settings", 35, "Exporting application settings...")
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT key, value, description, updated_at FROM settings ORDER BY key")
                settings_data = [dict(r) for r in rows]
                # Convert datetime to string for JSON serialization
                for s in settings_data:
                    if s.get("updated_at"):
                        s["updated_at"] = s["updated_at"].isoformat()

                # Also export host list (no credentials — those are in the dump)
                hosts = await conn.fetch(
                    "SELECT hostname, os_type, ssh_port, ssh_user, "
                    "notes, tags, is_control_node, allow_auto_reboot "
                    "FROM hosts ORDER BY hostname"
                )
                hosts_data = [dict(h) for h in hosts]
        except Exception as e:
            logger.warning(f"Could not export settings JSON (non-fatal): {e}")
            settings_data = []
            hosts_data = []

        settings_json = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "description": description,
            "settings": settings_data,
            "hosts_summary": hosts_data,
        }
        (backup_staging / "settings_export.json").write_text(
            json.dumps(settings_json, indent=2, default=str)
        )

        # ── Step 3: Ansible files ─────────────────────────────────────────
        _set_progress("ansible", 55, "Copying Ansible configuration...")
        ansible_backup = backup_staging / "ansible"
        ansible_backup.mkdir()
        includes_ansible = False
        if ANSIBLE_DIR.exists():
            for item in ANSIBLE_DIR.iterdir():
                dest = ansible_backup / item.name
                if item.is_file():
                    shutil.copy2(item, dest)
                    includes_ansible = True
                elif item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                    includes_ansible = True

        # ── Step 4: Encryption key (optional) ─────────────────────────────
        _set_progress("encryption", 65, "Handling encryption key...")
        includes_key = False
        if include_encryption_key:
            enc_key = os.getenv("PATCHPILOT_ENCRYPTION_KEY", "")
            if enc_key:
                key_data = {
                    "warning": "KEEP THIS FILE SECRET — it decrypts all stored SSH credentials",
                    "PATCHPILOT_ENCRYPTION_KEY": enc_key,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                }
                (backup_staging / "encryption_key.json").write_text(
                    json.dumps(key_data, indent=2)
                )
                includes_key = True
                logger.info("Encryption key included in backup")
            else:
                logger.warning("Encryption key not found in environment")

        # ── Step 4b: Reconstruct .env from container environment ──────────
        # INSTALL_DIR is a host path — it doesn't exist as a filesystem path
        # inside the container.  However, docker-compose already loaded every
        # variable from .env into the container environment via env_file:.env.
        # We write those values back out so the archive contains a complete,
        # working .env for the restore target.
        includes_env = False
        if uninstall_mode:
            known_vars = [
                # Database
                "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
                "POSTGRES_HOST", "POSTGRES_PORT",
                # Encryption
                "PATCHPILOT_ENCRYPTION_KEY",
                # Application
                "AUTO_REFRESH_INTERVAL", "DEFAULT_SSH_USER", "DEFAULT_SSH_PORT",
                "APP_BASE_URL", "ALLOWED_ORIGINS",
                # Backup
                "BACKUP_RETAIN_COUNT", "MAX_BACKUP_SIZE_MB",
                # Install
                "PATCHPILOT_INSTALL_MODE", "INSTALL_DIR",
            ]
            env_lines = [
                "# PatchPilot environment — reconstructed from running container",
                f"# Generated: {datetime.now(timezone.utc).isoformat()}",
                "",
                "# ── Database ──────────────────────────────────────────────",
            ]
            for var in known_vars:
                val = os.environ.get(var)
                if val is not None:
                    env_lines.append(f"{var}={val}")
            env_content = "\n".join(env_lines) + "\n"
            (backup_staging / ".env").write_text(env_content)
            includes_env = True
            logger.info("Reconstructed .env from container environment")
        _set_progress("metadata", 75, "Writing backup metadata...")
        meta = {
            "backup_name": backup_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "description": description,
            "postgres_version": _get_pg_version(),
            "app_version": _get_app_version(),
            "includes_ansible": includes_ansible,
            "includes_encryption_key": includes_key,
            "includes_env": includes_env,
            "uninstall_backup": uninstall_mode,
            "pg_host": PG_HOST,
            "pg_db": PG_DB,
            "format": "pg_custom",
            "restore_command": (
                f"pg_restore -h <host> -U {PG_USER} -d {PG_DB} "
                f"--clean --if-exists patchpilot.dump"
            ),
        }
        (backup_staging / "backup_metadata.json").write_text(
            json.dumps(meta, indent=2)
        )

        # ── Step 6: Create tar.gz archive ─────────────────────────────────
        _set_progress("archive", 85, "Creating backup archive...")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_staging, arcname=backup_name)

        archive_size = archive_path.stat().st_size
        logger.info(f"Backup archive created: {archive_path} ({_human_size(archive_size)})")

        # ── Step 6b: Write standalone encryption key file ─────────────────
        # Written *beside* the tarball so the operator can grab it without
        # cracking open the archive.  Retention policy never prunes .txt files.
        if includes_key:
            key_txt_name = (
                archive_path.name
                .replace(".tar.gz", "").replace(".tgz", "")
                + "_ENCRYPTION_KEY.txt"
            )
            key_txt_path = BACKUP_DIR / key_txt_name
            key_txt_path.write_text(
                f"# PatchPilot Encryption Key\n"
                f"# Backup: {archive_path.name}\n"
                f"# Created: {datetime.now(timezone.utc).isoformat()}\n"
                f"# WARNING: KEEP THIS FILE SECRET — it decrypts all stored SSH credentials\n\n"
                f"PATCHPILOT_ENCRYPTION_KEY={enc_key}\n"
            )
            logger.info(f"Encryption key file written: {key_txt_path}")

    # ── Step 7: Enforce retention policy ──────────────────────────────────
    _set_progress("retention", 95, "Applying retention policy...")
    await _enforce_retention()

    _set_progress("complete", 100, f"Backup complete: {archive_path.name}")
    return archive_path.name


def _backup_has_encryption_key(archive: Path) -> bool:
    """Check if a backup archive includes the encryption key."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("backup_metadata.json"):
                    f = tar.extractfile(member)
                    if f:
                        meta = json.loads(f.read().decode())
                        return meta.get("includes_encryption_key", False)
    except Exception:
        pass
    return False


async def _enforce_retention():
    """Delete oldest backups beyond the configured retain count.
    Reads retain count from the DB settings table (user-configurable via UI),
    falling back to the BACKUP_RETAIN_COUNT env var.
    Never delete backups that include the encryption key."""
    retain = await _get_retain_count()
    backups = _list_backup_archives(newest_first=True)
    for old_backup in backups[retain:]:
        try:
            if _backup_has_encryption_key(old_backup):
                logger.info(f"Retention skip (has encryption key): {old_backup.name}")
                continue
            old_backup.unlink()
            logger.info(f"Deleted old backup: {old_backup.name}")
        except Exception as e:
            logger.warning(f"Could not delete old backup {old_backup}: {e}")


# ---------------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------------
async def _run_restore(archive_path: Path) -> dict:
    """
    Core restore routine.
    Returns a summary dict of what was restored.
    """
    _check_pg_client_tools()

    if not archive_path.exists():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    archive_size_mb = archive_path.stat().st_size / (1024 * 1024)
    logger.info(f"Starting restore from: {archive_path.name} ({archive_size_mb:.1f} MB)")

    summary = {
        "archive": archive_path.name,
        "restored_at": datetime.now(timezone.utc).isoformat(),
        "database_restored": False,
        "ansible_restored": False,
        "settings_verified": False,
        "warnings": [],
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ── Step 1: Extract archive ────────────────────────────────────────
        _set_progress("extract", 10, "Extracting backup archive...")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmp_path)

        # Find the backup staging directory inside the archive
        extracted_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        if not extracted_dirs:
            raise RuntimeError("Backup archive appears to be empty or corrupt")
        staging = extracted_dirs[0]

        # Read metadata
        meta_file = staging / "backup_metadata.json"
        meta = {}
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            logger.info(f"Restoring backup from: {meta.get('created_at', 'unknown')}")
            logger.info(f"Original description: {meta.get('description', '')}")
        else:
            summary["warnings"].append("backup_metadata.json not found; proceeding anyway")

        dump_file = staging / "patchpilot.dump"
        if not dump_file.exists():
            raise FileNotFoundError("patchpilot.dump not found in backup archive")

        # ── Step 2: Terminate all connections to target DB ─────────────────
        _set_progress("connections", 20, "Terminating database connections...")
        try:
            # Connect to postgres (maintenance DB) to drop app DB
            admin_conn = await asyncpg.connect(
                host=PG_HOST, port=int(PG_PORT),
                user=PG_USER, password=PG_PASSWORD,
                database="postgres",  # connect to maintenance DB
                timeout=10
            )
            await admin_conn.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{PG_DB}'
                  AND pid <> pg_backend_pid()
            """)
            logger.info("All connections to target database terminated")

            # Close the app pool before we drop the database
            if _db_pool:
                await _db_pool.close()
                logger.info("Application connection pool closed")

            # ── Step 3: Drop and recreate DB ──────────────────────────────
            _set_progress("drop_db", 35, f"Dropping database '{PG_DB}'...")
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{PG_DB}"')
            logger.info(f"Dropped database: {PG_DB}")

            _set_progress("create_db", 40, f"Creating fresh database '{PG_DB}'...")
            await admin_conn.execute(f'CREATE DATABASE "{PG_DB}" OWNER "{PG_USER}"')
            logger.info(f"Created fresh database: {PG_DB}")
            await admin_conn.close()

        except Exception as e:
            raise RuntimeError(f"Failed to reset database: {e}")

        # ── Step 4: pg_restore ─────────────────────────────────────────────
        _set_progress("restore_db", 55, "Restoring database from dump...")
        pg_restore_cmd = [
            "pg_restore",
            "-h", PG_HOST, "-p", PG_PORT,
            "-U", PG_USER, "-d", PG_DB,
            "--no-password",
            "--verbose",
            "--exit-on-error",
            str(dump_file),
        ]
        result = subprocess.run(
            pg_restore_cmd, capture_output=True, text=True,
            env=_pg_env(), timeout=300
        )
        if result.returncode != 0:
            # pg_restore exits non-zero on warnings too; check for real errors
            stderr_lines = [l for l in result.stderr.split("\n") if "ERROR" in l]
            if stderr_lines:
                raise RuntimeError(f"pg_restore errors:\n" + "\n".join(stderr_lines))
            else:
                summary["warnings"].append("pg_restore had warnings (non-fatal)")
                logger.warning(f"pg_restore warnings:\n{result.stderr}")

        summary["database_restored"] = True
        logger.info("Database restore complete")

        # ── Step 5: Restore Ansible files ─────────────────────────────────
        _set_progress("ansible", 80, "Restoring Ansible configuration...")
        ansible_backup = staging / "ansible"
        if ansible_backup.exists() and any(ansible_backup.iterdir()):
            try:
                if ANSIBLE_DIR.exists():
                    # Snapshot current ansible files before overwriting
                    ansible_current_backup = BACKUP_DIR / f"ansible_pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    shutil.copytree(ANSIBLE_DIR, ansible_current_backup)
                    logger.info(f"Pre-restore ansible snapshot: {ansible_current_backup}")

                # Copy using dirs_exist_ok so we don't depend on rmtree succeeding.
                # This merges/overwrites individual files rather than replacing the
                # whole directory, which avoids FileExistsError if the dir persists.
                shutil.copytree(ansible_backup, ANSIBLE_DIR, dirs_exist_ok=True)
                summary["ansible_restored"] = True
                logger.info("Ansible configuration restored")

                # Always overwrite the playbook with the image-bundled version.
                # The playbook is app code not user data — an old backup version
                # would lose fixes/features added since. Only the hosts file is
                # user data worth restoring from backup.
                ansible_src = Path("/ansible-src/check-os-updates.yml")
                ansible_dst = ANSIBLE_DIR / "check-os-updates.yml"
                if ansible_src.exists():
                    shutil.copy2(ansible_src, ansible_dst)
                    logger.info("Playbook overwritten with current image version")
                else:
                    logger.warning("ansible-src playbook not found — backup version kept")
            except Exception as e:
                summary["warnings"].append(f"Ansible restore partial: {e}")
                logger.warning(f"Ansible restore warning: {e}")
        else:
            summary["warnings"].append("No Ansible files found in backup")

        # ── Step 5b: Restore encryption key to .env ───────────────────────
        # Critical: if the backup was made with a different encryption key than
        # the one currently running, all restored SSH credentials will be
        # unreadable.  Read encryption_key.json from the archive and rewrite
        # the PATCHPILOT_ENCRYPTION_KEY line in the on-disk .env so the
        # subsequent self-restart picks up the correct key.
        #
        # The install directory is mounted at /install in the container via
        # docker-compose (- .:/install:rw).  This is the only reliable path
        # to reach .env from inside the container.
        _set_progress("encryption_key", 87, "Restoring encryption key...")
        enc_key_file = staging / "encryption_key.json"

        # Primary: fixed container mount path (docker-compose: - .:/install:rw)
        # Fallback: INSTALL_DIR env var (for custom deployments)
        env_file_path = Path("/install/.env")
        if not env_file_path.exists() and INSTALL_DIR:
            env_file_path = INSTALL_DIR / ".env"

        if enc_key_file.exists():
            try:
                enc_key_data = json.loads(enc_key_file.read_text())
                backup_enc_key = enc_key_data.get("PATCHPILOT_ENCRYPTION_KEY", "")
                current_enc_key = os.getenv("PATCHPILOT_ENCRYPTION_KEY", "")

                if backup_enc_key and backup_enc_key != current_enc_key:
                    logger.warning(
                        "Backup encryption key differs from running key — "
                        "updating .env to match backup key"
                    )
                    if env_file_path.exists():
                        env_text = env_file_path.read_text()
                        if "PATCHPILOT_ENCRYPTION_KEY=" in env_text:
                            import re as _re
                            env_text = _re.sub(
                                r"^PATCHPILOT_ENCRYPTION_KEY=.*$",
                                f"PATCHPILOT_ENCRYPTION_KEY={backup_enc_key}",
                                env_text,
                                flags=_re.MULTILINE,
                            )
                        else:
                            env_text += f"\nPATCHPILOT_ENCRYPTION_KEY={backup_enc_key}\n"
                        env_file_path.write_text(env_text)
                        logger.info(f"Updated encryption key in {env_file_path}")
                        summary["encryption_key_updated"] = True
                    else:
                        # .env not accessible — write the key to /backups as a fallback
                        key_hint_path = BACKUP_DIR / "RESTORE_ENCRYPTION_KEY.txt"
                        key_hint_path.write_text(
                            f"PATCHPILOT_ENCRYPTION_KEY={backup_enc_key}\n\n"
                            "Add the above line to your .env file and restart the backend.\n"
                            "Without this, restored SSH credentials will be unreadable.\n"
                        )
                        summary["warnings"].append(
                            f"Encryption key mismatch but .env not writable. "
                            f"Key written to {key_hint_path}. "
                            "Add it to your .env and restart the backend."
                        )
                        logger.warning(
                            f"Could not find writable .env (tried /install/.env and "
                            f"{INSTALL_DIR / '.env' if INSTALL_DIR else 'N/A'}) — "
                            f"key hint written to {key_hint_path}"
                        )
                elif backup_enc_key == current_enc_key:
                    logger.info("Encryption key matches backup — no update needed")
                    summary["encryption_key_updated"] = False
            except Exception as e:
                summary["warnings"].append(f"Could not restore encryption key: {e}")
                logger.warning(f"Encryption key restore warning: {e}")
        else:
            summary["warnings"].append(
                "No encryption_key.json in backup — SSH credentials may be unreadable "
                "if this backup was created on a different install. "
                "If hosts show unreachable after restore, re-enter SSH keys in Settings."
            )
            logger.warning("encryption_key.json not found in backup archive")

        # ── Step 6: Verify settings post-restore ──────────────────────────
        _set_progress("verify", 92, "Verifying restored settings...")
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM settings")
                host_count = await conn.fetchval("SELECT COUNT(*) FROM hosts")
                logger.info(f"Verified: {count} settings rows, {host_count} hosts")
                summary["settings_count"] = count
                summary["host_count"] = host_count
                summary["settings_verified"] = True
        except Exception as e:
            summary["warnings"].append(f"Post-restore verification failed: {e}")

    _set_progress("complete", 100, "Restore complete")
    return summary


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/backup", tags=["Backup & Restore"])


@router.get("/status")
async def get_backup_status():
    """Return current maintenance mode status and operation progress."""
    retain = await _get_retain_count()
    return {
        "maintenance_mode": maintenance_mode,
        "maintenance_reason": maintenance_reason,
        "current_operation": current_operation,
        "progress": operation_progress,
        "backup_dir": str(BACKUP_DIR),
        "retain_count": retain,
        "docker_socket_available": _docker_available(),
    }


@router.get("/list", response_model=BackupListResponse)
async def list_backups():
    """List all available backup archives."""
    backups = []
    for archive in _list_backup_archives(newest_first=True):
        meta = {
            "postgres_version": "unknown",
            "app_version": "unknown",
            "includes_ansible": False,
            "includes_encryption_key": False,
            "description": "",
            "created_at": datetime.fromtimestamp(
                archive.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }
        # Try to read embedded metadata
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("backup_metadata.json"):
                        f = tar.extractfile(member)
                        if f:
                            embedded = json.loads(f.read().decode())
                            meta.update({
                                "postgres_version": embedded.get("postgres_version", "unknown"),
                                "app_version": embedded.get("app_version", "unknown"),
                                "includes_ansible": embedded.get("includes_ansible", False),
                                "includes_encryption_key": embedded.get("includes_encryption_key", False),
                                "description": embedded.get("description", ""),
                                "created_at": embedded.get("created_at", meta["created_at"]),
                            })
                        break
        except Exception:
            pass

        size = archive.stat().st_size
        backups.append(BackupMetadata(
            filename=archive.name,
            created_at=meta["created_at"],
            size_bytes=size,
            size_human=_human_size(size),
            postgres_version=meta["postgres_version"],
            app_version=meta["app_version"],
            includes_ansible=meta["includes_ansible"],
            includes_encryption_key=meta["includes_encryption_key"],
            description=meta["description"],
        ))

    retain = await _get_retain_count()
    return BackupListResponse(
        backups=backups,
        backup_dir=str(BACKUP_DIR),
        retain_count=retain,
    )


@router.post("/create")
async def create_backup(
    background_tasks: BackgroundTasks,
    description: str = "",
    include_encryption_key: bool = False,
):
    """
    Trigger a backup. Runs asynchronously in the background.
    Poll /api/backup/status for progress.
    """
    global current_operation

    if maintenance_mode:
        raise HTTPException(503, detail=f"System is busy: {maintenance_reason}")

    current_operation = "backup"

    async def _do_backup():
        try:
            await _enter_maintenance("Backup in progress — no writes accepted")
            filename = await _run_backup(description, include_encryption_key)
            logger.info(f"Backup completed successfully: {filename}")
        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            _set_progress("error", 0, f"Backup failed: {str(e)}")
        finally:
            await _exit_maintenance()

    background_tasks.add_task(_do_backup)
    return {"status": "started", "message": "Backup started in background. Poll /api/backup/status for progress."}


@router.get("/download/{filename}")
async def download_backup(filename: str):
    """Download a specific backup archive."""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    archive_path = BACKUP_DIR / safe_name
    if not archive_path.exists() or not _is_backup_file(safe_name):
        raise HTTPException(404, detail="Backup not found")
    return FileResponse(
        path=str(archive_path),
        filename=safe_name,
        media_type="application/gzip",
    )


@router.post("/upload")
async def upload_backup(file: UploadFile = File(...)):
    """Upload a backup archive to the server for restoration."""
    if not file.filename.endswith(".tar.gz"):
        raise HTTPException(400, detail="File must be a .tar.gz backup archive")

    safe_name = Path(file.filename).name
    if not _is_backup_file(safe_name):
        raise HTTPException(400, detail="Invalid backup filename format")

    dest = BACKUP_DIR / safe_name
    content = await file.read()

    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_BACKUP_SIZE_MB:
        raise HTTPException(413, detail=f"Backup too large ({size_mb:.1f} MB > {MAX_BACKUP_SIZE_MB} MB limit)")

    dest.write_bytes(content)
    logger.info(f"Uploaded backup: {safe_name} ({_human_size(len(content))})")
    return {"status": "uploaded", "filename": safe_name, "size_human": _human_size(len(content))}


@router.post("/restore")
def _schedule_self_restart(delay_seconds: int = 3) -> bool:
    """
    Spawn a detached docker:cli janitor that restarts the backend container
    after a short delay, giving the current request time to return a response.

    Returns True if the janitor was launched, False if docker.sock is not
    available (non-Docker deployment — caller should log a warning instead).
    """
    if not _docker_available():
        return False

    own_id = os.environ.get("HOSTNAME", "")
    if not own_id:
        logger.warning("HOSTNAME env var not set — cannot self-restart")
        return False

    script = f"sleep {delay_seconds} && docker restart {own_id}"
    result = subprocess.run(
        ["docker", "run", "--rm", "-d",
         "-v", "/var/run/docker.sock:/var/run/docker.sock",
         "docker:cli",
         "sh", "-c", script],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        logger.info(f"Self-restart janitor launched (delay={delay_seconds}s, target={own_id[:12]})")
        return True
    logger.warning(f"Self-restart janitor failed to launch: {result.stderr.strip()}")
    return False
    """
    Restore from a named backup archive.
    ⚠️  DESTRUCTIVE: drops and recreates the database.
    Must pass confirm=true to proceed.
    """
    global current_operation

    if not request.confirm:
        raise HTTPException(400, detail="Must set confirm=true to proceed with restore")

    if maintenance_mode:
        raise HTTPException(503, detail=f"System is busy: {maintenance_reason}")

    safe_name = Path(request.filename).name
    archive_path = BACKUP_DIR / safe_name
    if not archive_path.exists():
        raise HTTPException(404, detail=f"Backup '{safe_name}' not found")

    current_operation = "restore"

    async def _do_restore():
        try:
            await _enter_maintenance("Restore in progress — system temporarily unavailable")
            summary = await _run_restore(archive_path)
            logger.info(f"Restore completed: {json.dumps(summary, indent=2)}")

            # Rebuild connection pools immediately so endpoints work even
            # before a self-restart (or if self-restart is unavailable).
            try:
                await _rebuild_pool()
            except Exception as rp_e:
                logger.warning(f"Pool rebuild after restore: {rp_e}")

            # Schedule a self-restart so all in-process state (scheduler tasks,
            # cached settings, connection pools) is cleanly reinitialized.
            # The restart fires after a short delay so the progress poller can
            # collect the "complete" status before the container goes away.
            restarting = _schedule_self_restart(delay_seconds=5)
            if restarting:
                _set_progress(
                    "restarting", 100,
                    "Restore complete — restarting backend in 5 s… "
                    "The login page will be ready in ~15 seconds."
                )
            else:
                _set_progress(
                    "complete", 100,
                    "Restore complete. Running host check..."
                )
                # No self-restart available — trigger an immediate Ansible
                # check so the dashboard has fresh data as soon as the user
                # logs in, rather than waiting for the periodic timer.
                if _post_restore_callback:
                    try:
                        logger.info("Triggering post-restore Ansible check")
                        await _post_restore_callback()
                    except Exception as cb_e:
                        logger.warning(f"Post-restore check failed (non-fatal): {cb_e}")

        except Exception as e:
            logger.error(f"Restore failed: {e}", exc_info=True)
            _set_progress("error", 0, f"Restore failed: {str(e)}")
            try:
                await _rebuild_pool()
            except Exception:
                pass
        finally:
            await _exit_maintenance()

    background_tasks.add_task(_do_restore)
    return {
        "status": "started",
        "message": "Restore started. The application will be in maintenance mode until complete.",
        "warning": "All current data will be replaced. Poll /api/backup/status for progress.",
    }


@router.delete("/delete/{filename}")
async def delete_backup(filename: str):
    """Delete a backup archive from the server."""
    safe_name = Path(filename).name
    archive_path = BACKUP_DIR / safe_name
    if not archive_path.exists() or not _is_backup_file(safe_name):
        raise HTTPException(404, detail="Backup not found")
    archive_path.unlink()
    logger.info(f"Deleted backup: {safe_name}")
    return {"status": "deleted", "filename": safe_name}


@router.get("/health")
async def backup_health():
    """Quick health check for backup subsystem."""
    backup_count = len(_list_backup_archives())
    total_size = sum(f.stat().st_size for f in BACKUP_DIR.glob("*.tar.gz"))
    free = shutil.disk_usage(BACKUP_DIR).free
    return {
        "backup_dir": str(BACKUP_DIR),
        "backup_count": backup_count,
        "total_size_human": _human_size(total_size),
        "disk_free_human": _human_size(free),
        "pg_tools_available": all(
            subprocess.run(["which", t], capture_output=True).returncode == 0
            for t in ("pg_dump", "pg_restore")
        ),
        "docker_socket": _docker_available(),
        "install_dir_configured": bool(os.environ.get("INSTALL_DIR")),
        "install_dir": os.environ.get("INSTALL_DIR"),
        "env_file_found": True,   # always reconstructable from container environment
        # Expose env-level backup config so the frontend can detect
        # mismatches between env vars (set by install script / k8s manifest)
        # and DB-stored settings, and auto-correct when needed.
        "env_backup_storage_type": os.getenv("BACKUP_STORAGE_TYPE", "local"),
        "env_nfs_server": os.getenv("NFS_SERVER", ""),
        "env_nfs_share": os.getenv("NFS_SHARE", ""),
        "env_backup_retain_count": os.getenv("BACKUP_RETAIN_COUNT", "10"),
        "install_mode": os.getenv("PATCHPILOT_INSTALL_MODE", "docker").lower(),
    }


@router.post("/uninstall-backup")
async def create_uninstall_backup(
    background_tasks: BackgroundTasks,
    description: str = "Pre-uninstall backup",
):
    """
    Create a complete pre-uninstall backup.

    Differences from /create:
      - Encryption key is ALWAYS included (required for credentials to be
        recoverable after restore — SSH keys in the DB are useless without it)
      - .env file is bundled from INSTALL_DIR if configured
      - Archive is tagged '_uninstall' in the filename for easy identification
      - Returns the filename immediately after the background task completes
        so the uninstall modal can offer a direct download link

    On a fresh install, restore procedure:
      1. Copy .env from the archive to the new install directory
      2. Run docker compose up -d
      3. Use Settings → Backup & Restore → Upload & Restore to load the archive
    """
    global current_operation

    if maintenance_mode:
        raise HTTPException(503, detail=f"System is busy: {maintenance_reason}")

    current_operation = "backup"
    result: dict = {}

    async def _do_uninstall_backup():
        nonlocal result
        try:
            await _enter_maintenance("Pre-uninstall backup in progress")
            filename = await _run_backup(
                description=description,
                include_encryption_key=True,   # always forced in uninstall mode
                uninstall_mode=True,
            )
            result["filename"] = filename
            result["status"] = "complete"
            logger.info(f"Uninstall backup completed: {filename}")
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"Uninstall backup failed: {e}", exc_info=True)
            _set_progress("error", 0, f"Backup failed: {str(e)}")
        finally:
            await _exit_maintenance()

    # Run synchronously so the caller gets the filename back immediately
    # (the uninstall modal needs it for the download link before proceeding)
    await _do_uninstall_backup()

    if result.get("status") == "error":
        raise HTTPException(500, detail=result.get("error", "Backup failed"))

    filename = result["filename"]
    archive_path = BACKUP_DIR / filename
    size = archive_path.stat().st_size if archive_path.exists() else 0

    # Read metadata to report what was actually included
    included = {
        "database": True,
        "ansible": False,
        "encryption_key": False,
        "env_file": False,
    }
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("backup_metadata.json"):
                    f = tar.extractfile(member)
                    if f:
                        m = json.loads(f.read().decode())
                        included["ansible"] = m.get("includes_ansible", False)
                        included["encryption_key"] = m.get("includes_encryption_key", False)
                        included["env_file"] = m.get("includes_env", False)
                    break
    except Exception:
        pass

    warnings = []
    if not included["env_file"]:
        warnings.append(
            "INSTALL_DIR not configured — .env was NOT included. "
            "Add INSTALL_DIR=/path/to/patchpilot to your .env and recreate the backup, "
            "or manually copy your .env to the new install before restoring."
        )

    return {
        "status": "complete",
        "filename": filename,
        "size_human": _human_size(size),
        "download_url": f"/api/backup/download/{filename}",
        "included": included,
        "warnings": warnings,
        "restore_instructions": [
            "1. On your fresh install: copy .env from this archive to the install directory",
            "2. Run: docker compose up -d",
            "3. Go to Settings → Backup & Restore → Upload & Restore",
            "4. Upload this archive and confirm restore",
        ],
    }
