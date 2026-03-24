from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Optional, Deque
from datetime import datetime, timezone
import zoneinfo, os
from pathlib import Path
import asyncio
import logging
import psutil
import time
import uuid
import asyncpg
from collections import deque

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory ring buffer for backend log lines (last 2000 entries)
# ---------------------------------------------------------------------------
_LOG_RING_BUFFER: Deque[dict] = deque(maxlen=2000)

class _RingBufferHandler(logging.Handler):
    """Logging handler that pushes records into _LOG_RING_BUFFER."""
    LEVEL_MAP = {
        logging.DEBUG:    "debug",
        logging.INFO:     "info",
        logging.WARNING:  "warn",
        logging.ERROR:    "error",
        logging.CRITICAL: "error",
    }
    # Substrings that indicate high-frequency noise — skip to preserve buffer space
    _NOISE = ('"GET /health ', '"GET /api/hosts?', '"GET /api/stats?',
              '"GET /api/stats/', '"GET /api/schedules/active')
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            # Skip high-frequency polling / health endpoints
            if record.name == 'uvicorn.access':
                for noise in self._NOISE:
                    if noise in msg:
                        return
            _LOG_RING_BUFFER.append({
                "ts":   datetime.now(timezone.utc).isoformat(),
                "lvl":  self.LEVEL_MAP.get(record.levelno, "info"),
                "name": record.name,
                "msg":  msg,
            })
        except Exception:
            pass

# Attach ring-buffer handler to root logger so ALL loggers flow through it
_ring_handler = _RingBufferHandler()
_ring_handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
_ring_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_ring_handler)
logging.getLogger().setLevel(logging.INFO)      # third-party libs at INFO
logging.getLogger(__name__).setLevel(logging.DEBUG)  # our code at DEBUG

# ---------------------------------------------------------------------------
# Also intercept print() / sys.stdout so docker-style output lands in buffer
# ---------------------------------------------------------------------------
import sys as _sys

class _StdoutInterceptor:
    """Forwards write() calls to both the original stdout and the ring buffer."""
    def __init__(self, orig):
        self._orig = orig
        self._buf = ""
    def write(self, text):
        self._orig.write(text)
        self._orig.flush()
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                _LOG_RING_BUFFER.append({
                    "ts":   datetime.now(timezone.utc).isoformat(),
                    "lvl":  "info",
                    "name": "stdout",
                    "msg":  line,
                })
    def flush(self):
        self._orig.flush()
    def __getattr__(self, name):
        return getattr(self._orig, name)

_sys.stdout = _StdoutInterceptor(_sys.stdout)

from database import DatabaseClient
from ansible_runner import AnsibleRunner
from settings_api import router as settings_router, public_router as settings_public_router
from dependencies import get_db_pool
from auth import (router as auth_router, require_auth, require_full_admin,
                  require_write, ownership_filter, log_audit,
                  cleanup_expired_sessions, get_current_user)
from rbac import owner_id_or_param, verify_host_ownership_by_hostname
from schedules_api import router as schedules_router
from backup_restore import router as backup_router, set_pool as backup_set_pool, set_db_client as backup_set_db_client, set_post_restore_callback as backup_set_post_restore_callback
from setup_api import router as setup_router
from uninstall_api import router as uninstall_router
from update_checker import router as update_router, periodic_update_check
from license import router as license_router, license_set_pool, periodic_license_check, ensure_trial_for_existing_installs

# WebSocket connection manager for patch progress
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

# Initialize database client
db = DatabaseClient()

# Initialize Ansible runner
ansible = AnsibleRunner(
    playbook_path="/ansible/check-os-updates.yml",
    inventory_path="/ansible/hosts",
    db_client=db
)

# Track app start time for uptime
APP_START_TIME = time.time()

# Global lock — prevents two Ansible checks running simultaneously.
# A concurrent periodic check + manual refresh would cause one run to see
# all hosts as unreachable (SSH slots exhausted by the other run).
_ansible_check_lock = asyncio.Lock()
_ansible_check_lock_since: Optional[float] = None   # time.monotonic() when lock was acquired
_CHECK_LOCK_TIMEOUT = 600  # 10 min — auto-clear if stuck longer than this

# Flag set while a patch job (scheduled or manual) is actively running.
# The periodic background check skips its cycle while this is True so it
# doesn't race against the patch and produce false "unreachable" readings.
_ansible_patch_running = False
_ansible_patch_running_since: Optional[float] = None   # time.monotonic() when flag was set
_PATCH_FLAG_TIMEOUT = 1800  # 30 min — auto-clear if stuck longer than this

# Gate: scheduler waits until the first host check has completed on startup
# so it has accurate host status / total_updates before evaluating schedules.
_initial_check_done = asyncio.Event()

# Create FastAPI app
def _read_version() -> str:
    """Read version from VERSION file first, fall back to APP_VERSION env var.
    The VERSION file is updated by push_new_build.sh and is the most accurate
    source. APP_VERSION is baked into the image at build time and may be stale
    after an in-app upgrade that only swaps the image tag."""
    for path in ("VERSION", "/app/VERSION", "../VERSION"):
        try:
            ver = open(path).read().strip()
            if ver:
                return ver
        except FileNotFoundError:
            continue
    env_ver = os.getenv("APP_VERSION")
    if env_ver:
        return env_ver
    return "0.0.0-dev"

_APP_VERSION = _read_version()
app = FastAPI(title="PatchPilot API", version=_APP_VERSION)

# ── CORS configuration ────────────────────────────────────────────────────────
# ALLOWED_ORIGINS env var: comma-separated list of allowed origins.
# Examples:
#   ALLOWED_ORIGINS=*                                          (open — dev/default)
#   ALLOWED_ORIGINS=https://patchpilot.BLAH.com,https://patchpilot.lan
#
# When "*" is used, allow_credentials must be False per the CORS spec.
# When specific origins are listed, credentials (session cookies) work correctly.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*").strip()
_origins_list = [o.strip() for o in _raw_origins.split(",") if o.strip()]
_allow_creds = "*" not in _origins_list  # credentials require explicit origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins_list,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(settings_router)
app.include_router(settings_public_router)
app.include_router(auth_router)
app.include_router(schedules_router)
app.include_router(backup_router)
app.include_router(setup_router)
app.include_router(uninstall_router)
app.include_router(update_router)
app.include_router(license_router)

# Pydantic models
class PatchRequest(BaseModel):
    hostnames: List[str]
    become_password: Optional[str] = None

# Startup event
@app.on_event("startup")
async def startup_event():
    print("Starting PatchPilot...")
    await db.connect()
    print("Database connected (DatabaseClient)")
    
    # Create database pool for Settings API & Auth
    from dependencies import create_pool
    pool = await create_pool()
    
    # Wire pool into backup/restore router
    backup_set_pool(pool)
    license_set_pool(pool)
    # Wire DatabaseClient so restore can rebuild both pools after a DB drop/recreate
    backup_set_db_client(db)
    # Wire post-restore callback so restore triggers an immediate host check
    backup_set_post_restore_callback(run_ansible_check_task)
    
    # ── STEP 0: Sync bundled playbook to ansible volume ──────────────────────
    # The /ansible dir is a Docker volume mount from the host.  It may contain
    # a stale version of the playbook from a previous install, a restore, or
    # an old image.  Always overwrite with the version baked into this image
    # so the container is always running the current playbook.
    import shutil as _shutil
    _src_playbook = Path("/ansible-src/check-os-updates.yml")
    _dst_playbook = Path("/ansible/check-os-updates.yml")
    if _src_playbook.exists():
        try:
            _dst_playbook.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(_src_playbook, _dst_playbook)
            print(f"Playbook synced: {_src_playbook} → {_dst_playbook}")
        except Exception as _e:
            print(f"WARNING: Could not sync playbook: {_e}")
    else:
        print(f"WARNING: Bundled playbook not found at {_src_playbook}")

    # ── STEP 1: Core schema (hosts, packages, patch_history) ─────────────────
    # Must run BEFORE any column-check helpers that assume the tables exist.
    await ensure_core_tables(pool)

    # ── STEP 2: Auth tables (users, sessions, audit_log) ─────────────────────
    await run_auth_migration(pool)
    
    # Cleanup expired sessions
    await cleanup_expired_sessions(pool)
    
    # ── STEP 3: Settings table ────────────────────────────────────────────────
    await ensure_settings_table(pool)
    
    # ── STEP 4: Additive column migrations for existing installs ──────────────
    await ensure_audit_log_columns(pool)
    await ensure_hosts_columns(pool)
    await ensure_patch_history_columns(pool)
    
    # ── STEP 5: Scheduling tables ─────────────────────────────────────────────
    await ensure_schedules_tables(pool)

    # ── STEP 6: Saved SSH keys table ──────────────────────────────────────────
    await ensure_saved_ssh_keys_table(pool)

    # ── STEP 7: RBAC — ownership columns + role migration ─────────────────────
    await ensure_rbac_columns(pool)

    # ── STEP 7b: Ensure trial exists for pre-license upgrades/restores ────────
    await ensure_trial_for_existing_installs(pool)

    # ── STEP 8: Apply debug mode from settings ───────────────────────────────
    try:
        async with pool.acquire() as conn:
            debug_val = await conn.fetchval("SELECT value FROM settings WHERE key = 'debug_mode'")
        if debug_val and debug_val.lower() == 'true':
            logging.getLogger().setLevel(logging.DEBUG)
            for name in ('uvicorn', 'asyncpg', 'httpx', 'paramiko'):
                logging.getLogger(name).setLevel(logging.DEBUG)
            print("[STARTUP] Debug mode is ON (from settings)")
        else:
            print("[STARTUP] Debug mode is OFF")
    except Exception as e:
        print(f"[STARTUP] Could not read debug_mode setting: {e}")

    # ── STEP 9: Make email column nullable (no email functionality) ──────────
    try:
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ALTER COLUMN email DROP NOT NULL")
            await conn.execute("DROP INDEX IF EXISTS idx_users_email")
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique
                ON users(email) WHERE email IS NOT NULL
            """)
        print("[STARTUP] users.email column is now nullable")
    except Exception as e:
        print(f"[STARTUP] Email column migration note: {e}")
    
    # Start background task for periodic checks
    asyncio.create_task(periodic_ansible_check())
    print("[STARTUP] periodic_ansible_check loop launched")

    # Start background task for auto-patch schedules
    asyncio.create_task(schedule_checker_loop())
    print("[STARTUP] schedule_checker_loop launched")

    # Start background task for update checker
    async def _get_setting(key: str) -> Optional[str]:
        """Read a single setting value from the DB for the update checker."""
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT value FROM settings WHERE key = $1", key
                )
                return row
        except Exception:
            return None
    asyncio.create_task(periodic_update_check(_get_setting))
    print("[STARTUP] periodic_update_check loop launched")

    asyncio.create_task(periodic_license_check())
    print("[STARTUP] periodic_license_check loop launched")

    # Defer initial check until the DB has hosts to check.
    # After a restore + self-restart the pools need a few seconds to
    # reconnect and the restored data to become visible.  Rather than a
    # fixed sleep, poll until hosts exist (up to 60s ceiling).
    async def _deferred_initial_check():
        print("[STARTUP] Deferred initial check: waiting for hosts in DB...")
        for _ in range(12):          # 12 × 5s = 60s max wait
            await asyncio.sleep(5)
            try:
                hosts = await db.get_all_hosts()
                if hosts:
                    print(f"[STARTUP] Found {len(hosts)} host(s) — running initial Ansible check")
                    break
            except Exception:
                pass                 # pool not ready yet — retry
        try:
            await run_ansible_check_task()
            print(f"[STARTUP] Initial Ansible check completed")
        except Exception as e:
            print(f"[STARTUP] ERROR in initial Ansible check: {type(e).__name__}: {e}")
            logger.error(f"Deferred initial check error: {e}", exc_info=True)
        finally:
            _initial_check_done.set()
            print("[STARTUP] _initial_check_done event set — scheduler unblocked")
    asyncio.create_task(_deferred_initial_check())

@app.on_event("shutdown")
async def shutdown_event():
    await db.close()
    from dependencies import close_pool
    await close_pool()


async def ensure_core_tables(pool):
    """
    Create the canonical core tables on a fresh install.
    Uses IF NOT EXISTS so it is safe to run on every startup.
    All current columns are included here — the ensure_*_columns helpers
    below only handle ADDITIVE migrations for existing older installs.
    """
    try:
        async with pool.acquire() as conn:
            # hosts — central table, created first (others FK to it)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS hosts (
                    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    hostname          VARCHAR(255) UNIQUE NOT NULL,
                    ip_address        VARCHAR(45),
                    os_type           VARCHAR(50),
                    os_family         VARCHAR(50),
                    last_checked      TIMESTAMP WITH TIME ZONE,
                    status            VARCHAR(50)   DEFAULT 'unknown',
                    total_updates     INTEGER       DEFAULT 0,
                    reboot_required   BOOLEAN       DEFAULT FALSE,
                    allow_auto_reboot BOOLEAN       DEFAULT TRUE,
                    ssh_user          VARCHAR(100)  DEFAULT 'root',
                    ssh_port          INTEGER       DEFAULT 22,
                    ssh_key_type               VARCHAR(50)   DEFAULT 'default',
                    ssh_private_key_encrypted  BYTEA,
                    ssh_password_encrypted     BYTEA,
                    notes             TEXT,
                    tags              VARCHAR(255),
                    is_control_node   BOOLEAN       DEFAULT FALSE,
                    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hosts_hostname ON hosts(hostname)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status)"
            )

            # packages
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS packages (
                    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    host_id           UUID REFERENCES hosts(id) ON DELETE CASCADE,
                    package_name      VARCHAR(255) NOT NULL,
                    current_version   VARCHAR(100),
                    available_version VARCHAR(100),
                    update_type       VARCHAR(50),
                    detected_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(host_id, package_name)
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packages_host_id ON packages(host_id)"
            )

            # patch_history (includes output column from v0.9.1)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS patch_history (
                    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    host_id           UUID REFERENCES hosts(id) ON DELETE CASCADE,
                    execution_time    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    packages_updated  TEXT[],
                    success           BOOLEAN,
                    error_message     TEXT,
                    duration_seconds  INTEGER,
                    output            TEXT DEFAULT ''
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_history_host_id "
                "ON patch_history(host_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_history_execution_time "
                "ON patch_history(execution_time)"
            )

            print("Core tables ready (hosts, packages, patch_history)")
    except Exception as e:
        print(f"Core table creation FAILED: {e}")
        raise  # Fatal — cannot continue without schema


async def run_auth_migration(pool):
    """Create auth tables (users, sessions, audit_log) if they don't exist.

    SQL is inlined here — no dependency on a migrations/ file that may not
    be present inside the Docker image.
    """
    AUTH_MIGRATION_SQL = """
        -- Users table for authentication
        CREATE TABLE IF NOT EXISTS users (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            username      VARCHAR(50)  UNIQUE NOT NULL,
            email         VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role          VARCHAR(20)  NOT NULL DEFAULT 'viewer',
            is_active     BOOLEAN      DEFAULT true,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_login    TIMESTAMP WITH TIME ZONE
        );

        -- Sessions table
        CREATE TABLE IF NOT EXISTS sessions (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
            token      VARCHAR(255) UNIQUE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            ip_address VARCHAR(45),
            user_agent TEXT
        );

        -- Audit log
        CREATE TABLE IF NOT EXISTS audit_log (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
            username      VARCHAR(50),
            action        VARCHAR(100) NOT NULL,
            resource_type VARCHAR(50),
            resource_id   VARCHAR(255),
            details       JSONB,
            ip_address    VARCHAR(45),
            user_agent    TEXT,
            success       BOOLEAN DEFAULT true,
            timestamp     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_users_username      ON users(username);
        CREATE INDEX IF NOT EXISTS idx_users_email         ON users(email);
        CREATE INDEX IF NOT EXISTS idx_sessions_token      ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_audit_log_user_id   ON audit_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action    ON audit_log(action);

        -- Row Level Security (permissive — enforced at application layer)
        ALTER TABLE users     ENABLE ROW LEVEL SECURITY;
        ALTER TABLE sessions   ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_log  ENABLE ROW LEVEL SECURITY;
    """

    # RLS policies must be created outside a multi-statement string on some PG versions
    RLS_POLICIES = [
        ("users",     "Allow all operations on users",     "users"),
        ("sessions",  "Allow all operations on sessions",  "sessions"),
        ("audit_log", "Allow all operations on audit_log", "audit_log"),
    ]

    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'users'
                )
            """)
            if not exists:
                print("Running authentication migration (inlined)...")
                await conn.execute(AUTH_MIGRATION_SQL)
                # Create RLS policies only if they don't already exist
                for table, policy_name, _ in RLS_POLICIES:
                    policy_exists = await conn.fetchval("""
                        SELECT EXISTS (
                            SELECT 1 FROM pg_policies
                            WHERE tablename = $1 AND policyname = $2
                        )
                    """, table, policy_name)
                    if not policy_exists:
                        await conn.execute(
                            f'CREATE POLICY "{policy_name}" ON {table} FOR ALL USING (true)'
                        )
                print("Authentication tables created successfully")
            else:
                print("Authentication tables already exist")
    except Exception as e:
        print(f"Auth migration failed: {e}")
        raise  # Fatal — cannot create users without this schema


async def ensure_settings_table(pool):
    """Create settings table if it doesn't exist and seed defaults"""
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    description TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Seed defaults — runs for both new and existing installs (ON CONFLICT DO NOTHING)
            count = await conn.fetchval("SELECT COUNT(*) FROM settings")
            _default_settings = [
                ('refresh_interval',  '300',  'Dashboard auto-refresh interval in seconds'),
                ('default_ssh_user',  'root', 'Default SSH username for new hosts'),
                ('default_ssh_port',  '22',   'Default SSH port for new hosts'),
                ('schedule_timezone', 'UTC',  'Timezone for auto-patch schedule windows '
                                              '(e.g. America/Chicago, America/New_York, America/Los_Angeles)'),
                # ── Network / HTTPS settings ───────────────────────────────────
                ('app_base_url',     os.getenv('APP_BASE_URL', ''),
                 'Public base URL of this PatchPilot instance '
                 '(e.g. https://patchpilot.BLAH.com). '
                 'Used for CORS and self-referencing links.'),
                ('allowed_origins',  os.getenv('ALLOWED_ORIGINS', '*'),
                 'Comma-separated CORS origins. Use * for dev/open access or list '
                 'explicit URLs for production '
                 '(e.g. https://patchpilot.BLAH.com,https://patchpilot.lan). '
                 'Changes require a container restart to take effect.'),
                # ── macOS / System update settings ────────────────────────────
                ('macos_system_updates_enabled', os.getenv('MACOS_SYSTEM_UPDATES_ENABLED', 'false'),
                 'Enable macOS system (OS) updates via CLI during patch runs. Defaults to '
                 'false — softwareupdate over SSH is unreliable on newer macOS because the '
                 'daemon often hands off to the GUI notification manager and hangs. When '
                 'false, PatchPilot still detects available system updates but skips '
                 'installing them. Users install via System Settings instead.'),
                # ── macOS / App Store (mas) settings ──────────────────────────
                ('mas_enabled',          os.getenv('MAS_ENABLED', 'false'),
                 'Enable App Store (mas) updates on macOS hosts. Defaults to false — '
                 'mas in a headless SSH session requires an active GUI login and App Store '
                 'sign-in on the target Mac and will hang silently if those conditions '
                 'are not met. Set to true only after confirming mas works interactively.'),
                ('mas_excluded_ids',     os.getenv('MAS_EXCLUDED_IDS', '497799835'),
                 'Comma-separated App Store app IDs to skip during automated updates. '
                 'Default excludes Xcode (497799835) — it is enormous and rarely needs '
                 'automated updates. Add more IDs as needed.'),
                ('mas_per_app_timeout',  os.getenv('MAS_PER_APP_TIMEOUT', '600'),
                 'Hard per-app timeout in seconds on the remote host (default 600 = 10 min). '
                 'A timeout binary on the Mac kills a hung mas process so the run does not '
                 'block forever. Increase for very large apps.'),
                ('mas_timeout_seconds',  os.getenv('MAS_TIMEOUT_SECONDS', '7200'),
                 'Max seconds to wait for all App Store downloads per host (default 7200 = 2 h). '
                 'This is the Ansible async timeout — the overall ceiling for the task.'),
                # ── Windows / winget settings ──────────────────────────────────
                ('winget_excluded_ids',  os.getenv('WINGET_EXCLUDED_IDS', 'Microsoft.Edge'),
                 'Comma-separated winget package IDs to skip during update checks and patching. '
                 'Default excludes Microsoft.Edge — Edge uses a different install technology '
                 'than winget and cannot be upgraded via winget on most installations.'),
                ('winupdate_enabled',    os.getenv('WINUPDATE_ENABLED', 'false'),
                 'Enable Windows Update (PSWindowsUpdate) checks and patching on Windows hosts. '
                 'Defaults to false — requires PSWindowsUpdate module installed on the target host '
                 'and the PP-WinUpdate scheduled task created by Enable-PatchPilotSSH.ps1.'),
                # ── Update checker settings ───────────────────────────────────
                ('update_check_enabled', 'true',
                 'Enable periodic checks for new PatchPilot releases via GitHub.'),
                ('update_check_interval', '86400',
                 'How often to check for updates, in seconds (default 86400 = 24 hours, '
                 'minimum 3600 = 1 hour).'),
                # ── Debug / Logging ───────────────────────────────────────────
                ('debug_mode', 'false',
                 'Enable verbose debug logging. When true, all backend modules log at '
                 'DEBUG level. When false, only INFO and above are logged.'),
            ]
            await conn.executemany("""
                INSERT INTO settings (key, value, description) VALUES ($1, $2, $3)
                ON CONFLICT (key) DO NOTHING
            """, _default_settings)
            _status = "created" if count == 0 else "existing"
            print(f"Settings table ready ({_status}, new keys merged)")
    except Exception as e:
        print(f"Settings table init failed: {e}")


async def ensure_audit_log_columns(pool):
    """Add missing columns to audit_log if table predates auth migration"""
    columns_to_add = [
        ("user_id", "UUID REFERENCES users(id) ON DELETE SET NULL"),
        ("username", "VARCHAR(50)"),
        ("ip_address", "VARCHAR(45)"),
        ("user_agent", "TEXT"),
        ("success", "BOOLEAN DEFAULT true"),
    ]
    try:
        async with pool.acquire() as conn:
            for col_name, col_type in columns_to_add:
                exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'audit_log' AND column_name = $1
                    )
                """, col_name)
                if not exists:
                    await conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col_name} {col_type}")
                    print(f"Added column '{col_name}' to audit_log")
    except Exception as e:
        print(f"Audit log column check failed: {e}")


async def ensure_patch_history_columns(pool):
    """Auto-add newer columns to patch_history so fresh deployments don't need manual migrations"""
    columns_to_add = [
        ("output", "TEXT DEFAULT ''"),
    ]
    try:
        async with pool.acquire() as conn:
            for col_name, col_type in columns_to_add:
                exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'patch_history' AND column_name = $1
                    )
                """, col_name)
                if not exists:
                    await conn.execute(f"ALTER TABLE patch_history ADD COLUMN {col_name} {col_type}")
                    print(f"Added column '{col_name}' to patch_history")
                else:
                    print(f"patch_history.{col_name} already present")
    except Exception as e:
        print(f"patch_history column check failed: {e}")


async def ensure_hosts_columns(pool):
    """Add missing columns to hosts table and rename legacy column names."""
    columns_to_add = [
        ("allow_auto_reboot", "BOOLEAN DEFAULT TRUE"),
    ]
    try:
        async with pool.acquire() as conn:
            for col_name, col_type in columns_to_add:
                exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'hosts' AND column_name = $1
                    )
                """, col_name)
                if not exists:
                    await conn.execute(f"ALTER TABLE hosts ADD COLUMN {col_name} {col_type}")
                    print(f"Added column '{col_name}' to hosts table")

            # Rename ssh_private_key -> ssh_private_key_encrypted for existing installs
            old_key = await conn.fetchval("""
                SELECT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name='hosts' AND column_name='ssh_private_key')
            """)
            if old_key:
                await conn.execute("ALTER TABLE hosts RENAME COLUMN ssh_private_key TO ssh_private_key_encrypted")
                await conn.execute("ALTER TABLE hosts ALTER COLUMN ssh_private_key_encrypted TYPE BYTEA USING ssh_private_key_encrypted::bytea")
                print("Migrated hosts.ssh_private_key -> ssh_private_key_encrypted (BYTEA)")

            # Rename ssh_password -> ssh_password_encrypted for existing installs
            old_pwd = await conn.fetchval("""
                SELECT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name='hosts' AND column_name='ssh_password')
            """)
            if old_pwd:
                await conn.execute("ALTER TABLE hosts RENAME COLUMN ssh_password TO ssh_password_encrypted")
                await conn.execute("ALTER TABLE hosts ALTER COLUMN ssh_password_encrypted TYPE BYTEA USING ssh_password_encrypted::bytea")
                print("Migrated hosts.ssh_password -> ssh_password_encrypted (BYTEA)")

    except Exception as e:
        print(f"Hosts column check failed: {e}")


async def ensure_schedules_tables(pool):
    """Create auto-patch scheduling tables if they don't exist, and migrate column types"""
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS patch_schedules (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(100) NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    day_of_week TEXT NOT NULL DEFAULT 'sunday',
                    start_time TIME NOT NULL DEFAULT '02:00',
                    end_time TIME NOT NULL DEFAULT '04:00',
                    auto_reboot BOOLEAN DEFAULT FALSE,
                    become_password_encrypted TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    last_run TIMESTAMP WITH TIME ZONE,
                    last_status TEXT
                )
            """)
            # Migrate column types - swallow errors for already-correct columns
            type_migrations = [
                "ALTER TABLE patch_schedules ALTER COLUMN become_password_encrypted TYPE TEXT",
                "ALTER TABLE patch_schedules ALTER COLUMN day_of_week TYPE TEXT",
                "ALTER TABLE patch_schedules ALTER COLUMN last_status TYPE TEXT",
            ]
            for sql in type_migrations:
                try:
                    await conn.execute(sql)
                except Exception:
                    pass

            # Add retry_host_ids column with explicit existence check.
            # We do NOT silently swallow this one — we need to know if it worked.
            retry_col_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'patch_schedules'
                      AND column_name  = 'retry_host_ids'
                )
            """)
            if not retry_col_exists:
                await conn.execute(
                    "ALTER TABLE patch_schedules ADD COLUMN retry_host_ids UUID[]"
                )
                print("Added retry_host_ids column to patch_schedules")
            else:
                print("retry_host_ids column already present")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS patch_schedule_hosts (
                    schedule_id UUID REFERENCES patch_schedules(id) ON DELETE CASCADE,
                    host_id UUID REFERENCES hosts(id) ON DELETE CASCADE,
                    PRIMARY KEY (schedule_id, host_id)
                )
            """)
            print("Patch schedules tables ready")
    except Exception as e:
        print(f"Schedule tables init failed: {e}")


async def ensure_saved_ssh_keys_table(pool):
    """Create saved_ssh_keys table if it doesn't exist (missing from original core schema)."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS saved_ssh_keys (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name        VARCHAR(100) NOT NULL UNIQUE,
                    ssh_key_encrypted BYTEA NOT NULL,
                    is_default  BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_saved_ssh_keys_name ON saved_ssh_keys(name)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_saved_ssh_keys_default ON saved_ssh_keys(is_default)"
            )
            # Ensure only one default key PER USER.
            # Migration: drop the old global index (only one default across all users)
            # and create a per-user unique partial index instead.
            await conn.execute("""
                DO $$ BEGIN
                    -- Drop old global index if it exists
                    IF EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_one_default_key'
                    ) THEN
                        DROP INDEX idx_one_default_key;
                    END IF;
                    -- Create per-user unique index (one default per created_by)
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_one_default_key_per_user'
                    ) THEN
                        CREATE UNIQUE INDEX idx_one_default_key_per_user
                            ON saved_ssh_keys(created_by)
                            WHERE is_default = TRUE;
                    END IF;
                END $$;
            """)
            print("saved_ssh_keys table ready")
    except Exception as e:
        print(f"saved_ssh_keys table init failed: {e}")


async def ensure_rbac_columns(pool):
    """RBAC migration: add created_by ownership columns, migrate admin→full_admin role.

    Safe to run on every startup (all operations are idempotent).
    """
    try:
        async with pool.acquire() as conn:
            # ── 1. Add created_by columns to resource tables ──────────────
            for table in ('hosts', 'saved_ssh_keys', 'patch_schedules'):
                col_exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = $1 AND column_name = 'created_by'
                    )
                """, table)
                if not col_exists:
                    await conn.execute(f"""
                        ALTER TABLE {table}
                        ADD COLUMN created_by UUID REFERENCES users(id) ON DELETE SET NULL
                    """)
                    await conn.execute(f"""
                        CREATE INDEX IF NOT EXISTS idx_{table}_created_by
                        ON {table}(created_by)
                    """)
                    print(f"[RBAC] Added created_by column to {table}")

            # ── 2. Migrate role: admin → full_admin for the original admin ─
            #    Only upgrades users who are currently 'admin' AND were the
            #    first user created (i.e. the setup wizard user).  Subsequent
            #    'admin' users created via the new role model stay as 'admin'.
            migrated = await conn.fetchval("""
                UPDATE users SET role = 'full_admin', updated_at = NOW()
                WHERE role = 'admin'
                  AND id = (SELECT id FROM users ORDER BY created_at ASC LIMIT 1)
                RETURNING id
            """)
            if migrated:
                print(f"[RBAC] Migrated original admin user to full_admin (id={migrated})")

            # ── 3. Backfill created_by for existing resources ─────────────
            #    Assign all orphaned resources (created_by IS NULL) to the
            #    full_admin user so they don't become invisible after the
            #    ownership filter is enforced.
            fa_id = await conn.fetchval(
                "SELECT id FROM users WHERE role = 'full_admin' LIMIT 1"
            )
            if fa_id:
                for table in ('hosts', 'saved_ssh_keys', 'patch_schedules'):
                    updated = await conn.execute(f"""
                        UPDATE {table} SET created_by = $1
                        WHERE created_by IS NULL
                    """, fa_id)
                    if updated and updated != 'UPDATE 0':
                        print(f"[RBAC] Backfilled created_by on {table}: {updated}")

            # ── 4. Migrate any lingering 'operator' roles to 'viewer' ─────
            await conn.execute("""
                UPDATE users SET role = 'viewer', updated_at = NOW()
                WHERE role = 'operator'
            """)

            print("[RBAC] Role-based access control migration complete")
    except Exception as e:
        print(f"[RBAC] Migration failed: {e}")
        # Non-fatal — the app can still run, but ownership filtering
        # will fall back to showing everything until columns exist.


# Background task to run Ansible check
async def run_ansible_check_task(limit_hosts: list = None):
    """Background task to run Ansible check and update database.

    Uses a module-level asyncio.Lock so a manual refresh and the periodic
    background check can never run concurrently.  Without this, two Ansible
    processes hit the same SSH endpoints simultaneously — the slower one sees
    all connections occupied and reports every host as unreachable, writing
    that bad state to the DB before the first run can correct it.
    """
    global _ansible_patch_running, _ansible_patch_running_since, _ansible_check_lock_since
    if _ansible_check_lock.locked():
        # Auto-clear stuck check lock after timeout
        if _ansible_check_lock_since and (time.monotonic() - _ansible_check_lock_since) > _CHECK_LOCK_TIMEOUT:
            elapsed = int(time.monotonic() - _ansible_check_lock_since)
            print(f"[{datetime.now()}] WARNING: _ansible_check_lock stuck for {elapsed}s — force-releasing")
            try:
                _ansible_check_lock.release()
            except RuntimeError:
                pass  # already released by another path
            _ansible_check_lock_since = None
            # Don't proceed immediately — let the next periodic tick pick up
            # a clean run so we don't race with the old stuck coroutine.
            return
        else:
            print(f"[{datetime.now()}] Ansible check already running — skipping duplicate invocation")
            return

    if _ansible_patch_running:
        # Auto-clear stuck flag after timeout
        if _ansible_patch_running_since and (time.monotonic() - _ansible_patch_running_since) > _PATCH_FLAG_TIMEOUT:
            elapsed = int(time.monotonic() - _ansible_patch_running_since)
            print(f"[{datetime.now()}] WARNING: _ansible_patch_running stuck for {elapsed}s — auto-clearing")
            _ansible_patch_running = False
            _ansible_patch_running_since = None
        else:
            print(f"[{datetime.now()}] Patch in progress — skipping background check to avoid SSH conflicts")
            return

    async with _ansible_check_lock:
        _ansible_check_lock_since = time.monotonic()
        try:
            if limit_hosts:
                logger.debug(f"Running check for specific hosts: {limit_hosts}")
            else:
                logger.debug(f"Running check for all hosts")
            print(f"[{datetime.now()}] Running Ansible check...")
            # Ensure we're connected
            await db.connect()

            success, hosts_data = await ansible.run_check(limit_hosts=limit_hosts)
        finally:
            _ansible_check_lock_since = None
    if not success:
        print(f"Ansible check failed: {hosts_data.get('error', 'Unknown error')}")
        return
    # Log parsed results before writing so failed status is visible in logs
    for hostname, data in hosts_data.items():
        print(f"[CHECK] {hostname} → status={data.get('status')} updates={data.get('total_updates')} "
              f"os={data.get('os_family','?')}")
    # Update database with results
    for hostname, data in hosts_data.items():
        try:
            host = await db.upsert_host(
                hostname=hostname,
                ip_address=data.get("ip_address", ""),
                os_type=data.get("os_type", ""),
                os_family=data.get("os_family", ""),
                status=data.get("status", "unknown"),
                total_updates=data.get("total_updates", 0),
                reboot_required=data.get("reboot_required", False)
            )
            
            # Always clear old packages for this host
            if host:
                await db.delete_packages_for_host(host['id'])
                
                # Store new package details if any exist
                if data.get("update_details"):
                    for package in data.get("update_details", []):
                        await db.upsert_package(
                            host_id=host['id'],
                            package_name=package.get("package_name", ""),
                            current_version=package.get("current_version", ""),
                            available_version=package.get("available_version", ""),
                            update_type=package.get("update_type", "apt")
                        )
            
            print(f"Updated host: {hostname} - Status: {data.get('status')} - Updates: {data.get('total_updates')}")
        except Exception as e:
            print(f"Error updating host {hostname}: {e}")

    # ── Mark unchecked hosts as unreachable ─────────────────────────────────
    # If Ansible aborted early (e.g. a host was unreachable before
    # ignore_unreachable was added, or a fatal error stopped the play),
    # hosts that were never evaluated still carry their old stale status.
    # Compare the set of hosts we asked Ansible to check against what it
    # actually returned and mark the gap as unreachable.
    try:
        if limit_hosts:
            expected_hosts = set(limit_hosts)
        else:
            all_db_hosts = await db.get_all_hosts()
            expected_hosts = {h['hostname'] for h in all_db_hosts}
        checked_hosts = set(hosts_data.keys())
        unchecked = expected_hosts - checked_hosts
        if unchecked:
            print(f"[CHECK] {len(unchecked)} host(s) not in Ansible output — marking unreachable: {unchecked}")
            for hostname in unchecked:
                try:
                    existing = await db.get_host_by_hostname(hostname)
                    if existing:
                        await db.upsert_host(
                            hostname=hostname,
                            ip_address=existing.get('ip_address', ''),
                            os_type=existing.get('os_type', ''),
                            os_family=existing.get('os_family', ''),
                            status='unreachable',
                            total_updates=0,
                            reboot_required=False,
                        )
                        await db.delete_packages_for_host(existing['id'])
                        print(f"Updated host: {hostname} - Status: unreachable - Updates: 0 (not in Ansible output)")
                except Exception as e:
                    print(f"Error marking unchecked host {hostname}: {e}")
    except Exception as e:
        print(f"Warning: Failed to check for unchecked hosts: {e}")

    print(f"[{datetime.now()}] Ansible check completed")


# Background task to run ansible patch
async def run_ansible_patch_task(hostnames: List[str], become_password: Optional[str] = None):
    """Background task to run Ansible patch on specified hosts"""
    global _ansible_patch_running, _ansible_patch_running_since
    print(f"[{datetime.now()}] Running Ansible patch on: {', '.join(hostnames)}")
    await db.connect()
    
    _ansible_patch_running = True
    _ansible_patch_running_since = time.monotonic()
    try:
        # Broadcast start
        await manager.broadcast({
            "type": "start",
            "hosts": hostnames,
            "message": f"Starting patch for {len(hostnames)} host(s)..."
        })

        # Patch each host
        for hostname in hostnames:
            await manager.broadcast({
                "type": "progress",
                "hostname": hostname,
                "message": f"Patching {hostname}..."
            })
        
        start_time = datetime.now()
        
        # Create progress callback for real-time updates
        async def progress_callback(message):
            hostname = None
            import re
            host_match = re.search(r'(?:changed|ok|fatal|unreachable|skipping|failed):\s*\[([^\]]+)\]', message)
            if host_match:
                hostname = host_match.group(1)
            broadcast_data = {
                "type": "progress",
                "message": message
            }
            if hostname:
                broadcast_data["hostname"] = hostname
            await manager.broadcast(broadcast_data)
        
        success, results = await ansible.run_patch(
            limit_hosts=hostnames, 
            become_password=become_password,
            progress_callback=progress_callback
        )
        end_time = datetime.now()
        execution_seconds = (end_time - start_time).total_seconds()

        # Determine per-host actual patch success from Ansible output.
        ansible_output = results.get("output", "") if isinstance(results, dict) else ""
        actually_patched = _detect_hosts_actually_patched(ansible_output, hostnames)
        print(f"[INFO] actually_patched={actually_patched}, ansible_success={success}")

        # Record patch history for each host
        try:
            pool = db.pool
            async with pool.acquire() as conn:
                for hostname in hostnames:
                    host_row = await conn.fetchrow("SELECT id FROM hosts WHERE hostname = $1", hostname)
                    if host_row:
                        is_success = hostname in actually_patched
                        duration_secs = int(execution_seconds)
                        error_msg = None if is_success else (results.get("error", "Unknown error") if isinstance(results, dict) else str(results))
                        pkgs_updated = _extract_packages_updated(ansible_output, hostname)
                        print(f"[INFO] packages extracted for {hostname}: {len(pkgs_updated)} pkgs")
                        await conn.execute("""
                            INSERT INTO patch_history (host_id, success, packages_updated, duration_seconds, error_message, output)
                            VALUES ($1, $2, $3, $4, $5, $6)
                        """, host_row["id"], is_success, pkgs_updated, duration_secs, error_msg, ansible_output)
                        print(f"[INFO] Recorded patch_history for {hostname}: success={is_success}, pkgs={len(pkgs_updated)})")
        except Exception as e:
            print(f"[WARN] Failed to record patch history: {e}")

        if success or actually_patched:
            print(f"[{end_time}] Ansible patch completed (success={success}, patched={actually_patched})")
            await manager.broadcast({
                "type": "success",
                "message": "Patching completed successfully. Refreshing status..."
            })
        else:
            print(f"[{end_time}] Ansible patch failed: {results.get('error', 'Unknown error')}")
            await manager.broadcast({
                "type": "error",
                "message": f"Patch failed: {results.get('error', 'Unknown error') if isinstance(results, dict) else str(results)}"
            })

    finally:
        _ansible_patch_running = False
        _ansible_patch_running_since = None


    # Always re-run the check after patching — even on failure — so the dashboard
    # reflects the actual host state (up-to-date vs still needs updates).
    # Delay 30 s: softwareupdate/brew can leave SSH temporarily unresponsive
    # immediately after completing. Without the delay the check races in,
    # gets failed=1 in the RECAP, and stamps the host as "failed".
    await asyncio.sleep(30)
    await run_ansible_check_task(hostnames)
    await manager.broadcast({
        "type": "complete",
        "message": "All operations complete!"
    })


# Periodic check task
async def periodic_ansible_check():
    """Run Ansible check periodically, reading interval from settings"""
    while True:
        # Read interval from settings (default 120s)
        interval = 120
        try:
            pool = db.pool
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT value FROM settings WHERE key = 'refresh_interval'"
                )
                if row:
                    interval = max(30, int(row))  # Floor at 30s
        except Exception:
            pass
        await asyncio.sleep(interval)
        try:
            print(f"[{datetime.now()}] Periodic check loop firing (interval={interval}s)")
            await run_ansible_check_task()
        except Exception as e:
            print(f"[{datetime.now()}] ERROR in periodic_ansible_check: {type(e).__name__}: {e}")
            logger.error(f"Periodic check loop error: {e}", exc_info=True)


# =========================================================================
# AUTO-PATCH SCHEDULE CHECKER
# =========================================================================

async def schedule_checker_loop():
    """Background loop that checks for due auto-patch schedules every 60 seconds"""
    # Wait until the initial host check has completed so the scheduler has
    # accurate host status and total_updates before evaluating any schedule.
    # Falls back to a 120s ceiling so a broken initial check can't block forever.
    try:
        await asyncio.wait_for(_initial_check_done.wait(), timeout=120)
        logger.info("[Scheduler] Initial host check complete — scheduler starting")
    except asyncio.TimeoutError:
        logger.warning("[Scheduler] Timed out waiting for initial host check (120s) — starting anyway")
    while True:
        try:
            await check_and_run_schedules()
        except Exception as e:
            logger.error(f"Schedule checker error: {e}")
        await asyncio.sleep(60)


def _parse_unreachable_hostnames(output: str, attempted_hostnames: list) -> list:
    """Parse ansible PLAY RECAP output and return hostnames that were unreachable."""
    import re
    unreachable = []
    for line in output.splitlines():
        m = re.match(r'^([^\s:]+)\s*:\s*ok=\d+.*unreachable=(\d+)', line)
        if m and int(m.group(2)) > 0:
            hostname = m.group(1)
            # Match against attempted list (ansible may truncate long names)
            for h in attempted_hostnames:
                if h == hostname or h.startswith(hostname) or hostname.startswith(h.split('.')[0]):
                    if h not in unreachable:
                        unreachable.append(h)
                    break
    return unreachable


def _detect_hosts_actually_patched(output: str, hostnames: list) -> set:
    """
    Determine which hosts actually had their packages updated, even when Ansible
    exits non-zero due to a post-update SSH failure (e.g. temp key race condition).

    Logic: scan the output task-by-task. If a host has a 'changed:' or 'ok:' line
    inside the 'Apply updates' task block, the apt/yum/brew command ran to completion
    on that host — regardless of whether subsequent tasks (reboot check, etc.) were
    UNREACHABLE because the SSH connection dropped after the packages were installed.

    Returns a set of hostname strings that were successfully patched.
    """
    import re
    patched = set()
    in_apply_task = False

    for line in output.splitlines():
        stripped = line.strip()

        # Detect the "Apply updates" task header
        if re.match(r'^TASK\s*\[', stripped):
            in_apply_task = 'apply' in stripped.lower() and 'update' in stripped.lower()
            continue

        if in_apply_task:
            # ONLY count changed: — ok: means apt ran but installed nothing.
            # This happens when the check playbook read a stale apt cache that showed
            # packages pending, but the patch playbook runs apt-get update first and
            # gets a fresh view where nothing needs upgrading.
            # Accepting ok: here was the root cause of false-positive "patched" reports.
            m = re.match(r'^changed:\s*\[([^\]]+)\]', stripped)
            if m:
                ansible_host = m.group(1)
                for h in hostnames:
                    if h == ansible_host or ansible_host == h:
                        patched.add(h)
                        break
                else:
                    patched.add(ansible_host)
            # Log ok: explicitly so the stale-cache condition is visible in logs
            m_ok = re.match(r'^ok:\s*\[([^\]]+)\]', stripped)
            if m_ok:
                print(f"[WARN] apt task reported ok (no changes) for {m_ok.group(1)} — "
                      f"host may have stale check cache or packages already current")

    return patched


def _extract_packages_updated(output: str, hostname: str) -> list:
    """
    Parse the raw Ansible output and return a list of package names that were
    installed/upgraded on the given host during the patch run.

    Ansible with -v emits lines like:
        changed: [192.168.1.50] => {"changed": true, "stdout": "...", "stdout_lines": [...]}

    The stdout_lines array contains apt output including:
        "Setting up libssl3:amd64 (3.0.2-0ubuntu1.18) ..."
        "Unpacking libssl3:amd64 (3.0.2-0ubuntu1.18) ..."

    We collect unique package names from "Setting up" lines (which means the
    package was fully installed), falling back to "Unpacking" if nothing else
    is found. Strip arch suffixes (:amd64, :arm64, etc.) for a clean name.
    """
    import re as _re
    import json as _json

    packages = []
    seen = set()

    # Strategy 1: find the JSON blob for this host in the Apply updates task
    # The line starts with "changed: [hostname] =>" and may be very long
    in_apply_task = False
    for line in output.splitlines():
        stripped = line.strip()

        if _re.match(r'^TASK\s*\[', stripped):
            in_apply_task = 'apply' in stripped.lower() and 'update' in stripped.lower()
            continue

        if not in_apply_task:
            continue

        # Match "changed: [hostname] => {json...}"
        m = _re.match(r'^(?:changed|ok):\s*\[([^\]]+)\]\s*=>\s*(\{.*)', stripped)
        if not m:
            continue

        task_host = m.group(1)
        # Accept if the hostname matches by IP or name
        if task_host != hostname and not hostname.startswith(task_host) and not task_host.startswith(hostname.split('.')[0]):
            continue

        json_str = m.group(2)
        try:
            data = _json.loads(json_str)
        except Exception:
            # JSON is truncated on one line — try to scrape stdout_lines directly
            data = {}

        stdout_lines = data.get('stdout_lines', [])

        for sline in stdout_lines:
            s = sline.strip()
            # "Setting up pkg:arch (ver) ..."  — package fully installed
            pkg_m = _re.match(r'^Setting up\s+([\w\-\.+]+)(?::\w+)?\s+\(([^)]+)\)', s)
            if pkg_m:
                pkg = pkg_m.group(1)
                ver = pkg_m.group(2)
                key = f"{pkg}={ver}"
                if key not in seen:
                    seen.add(key)
                    packages.append(f"{pkg} ({ver})")

        # If "Setting up" found nothing, fall back to "Unpacking"
        if not packages:
            for sline in stdout_lines:
                s = sline.strip()
                pkg_m = _re.match(r'^Unpacking\s+([\w\-\.+]+)(?::\w+)?\s+\(([^)]+)\)', s)
                if pkg_m:
                    pkg = pkg_m.group(1)
                    ver = pkg_m.group(2)
                    key = f"{pkg}={ver}"
                    if key not in seen:
                        seen.add(key)
                        packages.append(f"{pkg} ({ver})")

        if packages:
            return packages

    # Strategy 2: scan raw output for "Setting up" if JSON parsing missed it
    # (occurs when Ansible stdout callback emits multi-line output)
    for line in output.splitlines():
        pkg_m = _re.match(r'^\s*Setting up\s+([\w\-\.+]+)(?::\w+)?\s+\(([^)]+)\)', line)
        if pkg_m:
            pkg = pkg_m.group(1)
            ver = pkg_m.group(2)
            key = f"{pkg}={ver}"
            if key not in seen:
                seen.add(key)
                packages.append(f"{pkg} ({ver})")

    # Strategy 3: winget output -- Ansible emits the entire changed/ok line as
    # ONE line with an embedded JSON blob containing "stdout_lines": [...].
    # Each entry in stdout_lines is a separate winget output line.
    # We look for "Found <n> [PackageId] Version X" followed by
    # "Successfully installed" to count successful winget upgrades.
    if not packages:
        _winget_lines = []
        for line in output.splitlines():
            # Try to extract stdout_lines from JSON in "changed: [host] => {json}"
            json_m = _re.search(r'=>\s*(\{.*)', line)
            if json_m:
                try:
                    data = _json.loads(json_m.group(1))
                    _winget_lines.extend(data.get('stdout_lines', []))
                except Exception:
                    pass
            # Fallback: split on JSON array separators if Found + Success on same line
            if not _winget_lines and 'Found' in line and 'Successfully installed' in line:
                parts = _re.split(r'",\s*"', line)
                _winget_lines.extend(parts)

        _last_found_pkg = None
        for s in _winget_lines:
            s = s.strip().strip('"')
            found_m = _re.search(r'Found\s+.+?\[([^\]]+)\]\s+Version\s+([\d][\d\.]*)', s)
            if found_m:
                _last_found_pkg = (found_m.group(1), found_m.group(2))
            elif 'Successfully installed' in s and _last_found_pkg:
                pkg_id, ver = _last_found_pkg
                key = f"{pkg_id}={ver}"
                if key not in seen:
                    seen.add(key)
                    packages.append(f"{pkg_id} ({ver})")
                _last_found_pkg = None
            elif _re.search(r'Installer failed|install technology is different', s):
                _last_found_pkg = None

    # Strategy 4: Homebrew output -- look for "Upgrading <pkg>" lines followed by
    # version info. Brew upgrade output contains lines like:
    #   ==> Upgrading python@3.12
    #     3.12.7 -> 3.12.8
    # Or simpler: "==> Upgrading <n> outdated packages:" followed by "pkg ver -> ver" lines
    if not packages:
        _winget_lines = []
        for line in output.splitlines():
            json_m = _re.search(r'=>\s*(\{.*)', line)
            if json_m:
                try:
                    data = _json.loads(json_m.group(1))
                    _winget_lines.extend(data.get('stdout_lines', []))
                except Exception:
                    pass

        _last_brew_pkg = None
        for s in _winget_lines:
            s = s.strip()
            # "==> Upgrading python@3.12"
            brew_up = _re.match(r'^==> Upgrading\s+([\w\-\.@/]+)', s)
            if brew_up:
                _last_brew_pkg = brew_up.group(1)
                continue
            # "  3.12.7 -> 3.12.8" (version line after Upgrading)
            if _last_brew_pkg:
                ver_m = _re.match(r'^\s*([\d][\d\.]*)\s+->\s+([\d][\d\.]*)', s)
                if ver_m:
                    new_ver = ver_m.group(2)
                    key = f"{_last_brew_pkg}={new_ver}"
                    if key not in seen:
                        seen.add(key)
                        packages.append(f"{_last_brew_pkg} ({new_ver})")
                    _last_brew_pkg = None
                    continue
            # "pkg ver -> ver" on a single line (compact format)
            brew_compact = _re.match(r'^([\w\-\.@/]+)\s+([\d][\d\.]*)\s+->\s+([\d][\d\.]*)', s)
            if brew_compact:
                pkg = brew_compact.group(1)
                new_ver = brew_compact.group(3)
                key = f"{pkg}={new_ver}"
                if key not in seen:
                    seen.add(key)
                    packages.append(f"{pkg} ({new_ver})")

    return packages


async def check_and_run_schedules():
    """Check if any schedules are due to run, and retry unreachable hosts within the same window."""
    from encryption_utils import decrypt_credential

    pool = db.pool

    # Determine timezone: DB setting > TZ env var > UTC
    try:
        async with pool.acquire() as _c:
            tz_row = await _c.fetchrow("SELECT value FROM settings WHERE key = 'schedule_timezone'")
        tz_name = (tz_row['value'].strip() if tz_row and tz_row['value'] else None) or os.environ.get('TZ', 'UTC')
        local_tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz_name = 'UTC'
        local_tz = zoneinfo.ZoneInfo('UTC')

    now_utc = datetime.now(timezone.utc)          # for DB timestamps
    now = now_utc.astimezone(local_tz)            # local wall-clock time
    current_day = now.strftime('%A').lower()
    current_time = now.time().replace(tzinfo=None)

    # Log the scheduler heartbeat every cycle so it's easy to confirm it's running
    # and using the correct timezone.  This also makes timezone misconfiguration obvious.
    logger.info(
        f"[Scheduler] tick — tz={tz_name}  local={now.strftime('%Y-%m-%d %H:%M:%S')}  "
        f"day={current_day}  utc={now_utc.strftime('%H:%M:%S')}"
    )

    async with pool.acquire() as conn:
        schedules = await conn.fetch("""
            SELECT s.*,
                   array_agg(sh.host_id) FILTER (WHERE sh.host_id IS NOT NULL) as host_ids
            FROM patch_schedules s
            LEFT JOIN patch_schedule_hosts sh ON s.id = sh.schedule_id
            WHERE s.enabled = TRUE
            GROUP BY s.id
        """)

        for sched in schedules:
            # If a patch is already in progress, skip all schedule evaluation
            # this tick — we'll pick it up on the next 60s cycle.
            if _ansible_patch_running:
                logger.debug(
                    f"[Scheduler] Patch already in progress — "
                    f"deferring all schedule evaluation to next tick"
                )
                break

            # Must be the right day
            sched_days = [d.strip().lower() for d in sched['day_of_week'].split(',')]
            if current_day not in sched_days:
                logger.debug(f"[Scheduler] '{sched['name']}' skipped — today={current_day} not in {sched_days}")
                continue

            # Must be within the configured time window
            in_window = sched['start_time'] <= current_time <= sched['end_time']
            logger.info(
                f"[Scheduler] '{sched['name']}' window={sched['start_time']}–{sched['end_time']}  "
                f"now={current_time}  in_window={in_window}"
            )
            if not in_window:
                # Window closed — clear any pending retry list so it doesn't carry to tomorrow
                # Use dict() cast so key access is safe regardless of asyncpg Record version
                sched_dict = dict(sched)
                if sched_dict.get('retry_host_ids'):
                    try:
                        await conn.execute(
                            "UPDATE patch_schedules SET retry_host_ids = NULL WHERE id = $1", sched['id']
                        )
                    except Exception:
                        pass
                continue

            # --- Determine which hosts need patching this cycle ---
            # The scheduler is stateless per-tick: it looks at each host's
            # CURRENT status and decides whether to patch.  No "already ran
            # today" gate — if a host picks up new updates mid-window it
            # gets patched again.
            host_ids = sched['host_ids'] or []
            if not host_ids:
                continue

            # Also honour the explicit retry list from a prior 'partial' run
            # (hosts that were unreachable last attempt).
            sched_dict = dict(sched)
            retry_ids = set(sched_dict.get('retry_host_ids') or [])

            needs_patch_ids = []
            for hid in host_ids:
                # If host is on the retry list, always attempt it regardless
                # of DB status — Ansible will determine reachability.
                if hid in retry_ids:
                    needs_patch_ids.append(hid)
                    continue

                # Otherwise, check current host state
                host_row = await conn.fetchrow("""
                    SELECT total_updates, status
                    FROM hosts WHERE id = $1
                """, hid)
                if not host_row:
                    continue
                if host_row['status'] in ('offline', 'unreachable'):
                    continue
                if (host_row['total_updates'] or 0) <= 0:
                    continue
                needs_patch_ids.append(hid)

            if not needs_patch_ids:
                logger.debug(
                    f"Schedule '{sched['name']}': no hosts need patching "
                    f"(all patched, offline, or 0 updates) — skipping"
                )
                continue

            logger.info(
                f"Schedule '{sched['name']}': {len(needs_patch_ids)}/{len(host_ids)} "
                f"host(s) need patching this cycle"
            )
            target_host_ids = needs_patch_ids

            # Resolve host IDs → hostnames.
            # For retries we intentionally do NOT filter on DB status — the check scan
            # runs every 5+ minutes so its status may be stale.  Let Ansible determine
            # actual reachability; if the host is still down it will appear in PLAY RECAP
            # as unreachable and get stored back into retry_host_ids for the next cycle.
            hostnames = []
            target_host_ids_for_patch = []
            for hid in target_host_ids:
                row = await conn.fetchrow(
                    "SELECT hostname FROM hosts WHERE id = $1", hid
                )
                if not row:
                    continue
                hostnames.append(row['hostname'])
                target_host_ids_for_patch.append(hid)

            if not hostnames:
                logger.info(f"Schedule '{sched['name']}': no valid hosts resolved, skipping cycle")
                continue

            # Decrypt become password
            become_password = None
            if sched['become_password_encrypted']:
                try:
                    become_password = decrypt_credential(sched['become_password_encrypted'])
                except Exception as e:
                    logger.error(f"Failed to decrypt schedule password: {e}")
                    await conn.execute(
                        "UPDATE patch_schedules SET last_run = $1, last_status = 'error' WHERE id = $2",
                        now_utc, sched['id']
                    )
                    try:
                        await conn.execute(
                            "UPDATE patch_schedules SET retry_host_ids = NULL WHERE id = $1", sched['id']
                        )
                    except Exception:
                        pass
                    continue

            # Mark as running and update last_run timestamp.
            # CRITICAL: last_run and last_status are set in their own statement first
            # so a missing retry_host_ids column can never prevent them from being written.
            await conn.execute(
                "UPDATE patch_schedules SET last_run = $1, last_status = 'running' WHERE id = $2",
                now_utc, sched['id']
            )
            # Clear retry list separately — safe to fail if column not yet present
            try:
                await conn.execute(
                    "UPDATE patch_schedules SET retry_host_ids = NULL WHERE id = $1",
                    sched['id']
                )
            except Exception as _e:
                logger.debug(f"retry_host_ids clear skipped (column may not exist yet): {_e}")

            logger.info(f"Auto-patch schedule '{sched['name']}' triggered for: {hostnames}")

            asyncio.create_task(
                run_scheduled_patch(sched['id'], hostnames, become_password, pool, local_tz)
            )


async def run_scheduled_patch(schedule_id, hostnames, become_password, pool, local_tz=None):
    """Run a scheduled patch, then store any unreachable host IDs for in-window retry."""
    global _ansible_patch_running, _ansible_patch_running_since
    if local_tz is None:
        local_tz = zoneinfo.ZoneInfo('UTC')

    _ansible_patch_running = True
    _ansible_patch_running_since = time.monotonic()
    final_status = 'error'  # default — overwritten on success
    try:
        logger.info(f"[Schedule {schedule_id}] Ansible patch starting for: {hostnames}")
        success, results = await _run_patch_and_return_results(hostnames, become_password)
        logger.info(f"[Schedule {schedule_id}] Ansible patch returned: success={success}")

        output = results.get('output', '') if isinstance(results, dict) else ''
        unreachable_names = _parse_unreachable_hostnames(output, hostnames)

        if unreachable_names:
            final_status = 'partial'
        else:
            final_status = 'success'

        logger.info(f"[Schedule {schedule_id}] Final status will be: {final_status} "
                    f"(unreachable={unreachable_names})")

        # ── Record patch_history for each host (mirrors manual-patch logic) ──
        try:
            elapsed = int(time.monotonic() - _ansible_patch_running_since) if _ansible_patch_running_since else 0
            actually_patched = _detect_hosts_actually_patched(output, hostnames)
            async with pool.acquire() as conn:
                for hostname in hostnames:
                    host_row = await conn.fetchrow(
                        "SELECT id FROM hosts WHERE hostname = $1", hostname
                    )
                    if host_row:
                        is_success = hostname in actually_patched
                        error_msg = None if is_success else (
                            results.get("error", "Unknown error")
                            if isinstance(results, dict) else str(results)
                        )
                        pkgs_updated = _extract_packages_updated(output, hostname)
                        await conn.execute("""
                            INSERT INTO patch_history
                                (host_id, success, packages_updated,
                                 duration_seconds, error_message, output)
                            VALUES ($1, $2, $3, $4, $5, $6)
                        """, host_row["id"], is_success, pkgs_updated,
                            elapsed, error_msg, output)
                        logger.info(
                            f"[Schedule {schedule_id}] Recorded patch_history "
                            f"for {hostname}: success={is_success}, "
                            f"pkgs={len(pkgs_updated)}"
                        )
        except Exception as hist_err:
            logger.error(
                f"[Schedule {schedule_id}] Failed to record patch_history: {hist_err}"
            )

        # Store unreachable host IDs for in-window retry
        if unreachable_names:
            unreachable_ids = []
            async with pool.acquire() as conn:
                for name in unreachable_names:
                    row = await conn.fetchrow("SELECT id FROM hosts WHERE hostname = $1", name)
                    if row:
                        unreachable_ids.append(row['id'])
                try:
                    await conn.execute(
                        "UPDATE patch_schedules SET retry_host_ids = $2 WHERE id = $1",
                        schedule_id, unreachable_ids if unreachable_ids else None
                    )
                except Exception as _e:
                    logger.warning(f"Could not write retry_host_ids: {_e}")

    except Exception as e:
        logger.error(f"[Schedule {schedule_id}] Exception during patch: {e}", exc_info=True)
        final_status = 'error'
    finally:
        _ansible_patch_running = False
        _ansible_patch_running_since = None
        # Always write the final status — this runs even if an exception occurred above.
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE patch_schedules SET last_status = $1 WHERE id = $2",
                    final_status, schedule_id
                )
                if final_status != 'partial':
                    try:
                        await conn.execute(
                            "UPDATE patch_schedules SET retry_host_ids = NULL WHERE id = $1",
                            schedule_id
                        )
                    except Exception:
                        pass
            logger.info(f"[Schedule {schedule_id}] Status written: {final_status}")
        except Exception as _db_e:
            logger.error(f"[Schedule {schedule_id}] FAILED to write final status '{final_status}': {_db_e}")

        # Post-patch check: now that _ansible_patch_running is False the check can proceed
        asyncio.create_task(run_ansible_check_task(hostnames))


async def _run_patch_and_return_results(hostnames, become_password):
    """Run an ansible patch using the global runner and return (success, results).
    Callers are responsible for setting/clearing _ansible_patch_running.
    """
    await manager.broadcast({
        "type": "start",
        "hosts": hostnames,
        "message": f"[Scheduled] Starting patch for {len(hostnames)} host(s)..."
    })

    async def progress_callback(message):
        import re
        host_match = re.search(r'(?:changed|ok|fatal|unreachable|skipping|failed):\s*\[([^\]]+)\]', message)
        hostname = host_match.group(1) if host_match else None
        data = {"type": "progress", "message": message}
        if hostname:
            data["hostname"] = hostname
        await manager.broadcast(data)

    success, results = await ansible.run_patch(
        limit_hosts=hostnames,
        become_password=become_password,
        progress_callback=progress_callback
    )

    if success:
        await manager.broadcast({"type": "success", "message": "Scheduled patch completed."})
        await manager.broadcast({"type": "complete", "message": "All operations complete!"})
    else:
        await manager.broadcast({
            "type": "error",
            "message": f"Scheduled patch failed: {results.get('error', '') if isinstance(results, dict) else str(results)}"
        })

    return success, results



# API Endpoints
# WebSocket endpoint for real-time patch progress
@app.websocket("/ws/patch-progress")
async def websocket_patch_progress(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/health")
async def health():
    """Kubernetes liveness/readiness probe endpoint."""
    return {"status": "ok", "version": _APP_VERSION}

@app.get("/")
@app.get("/api")
@app.get("/api/")
async def root():
    return {"message": "PatchPilot API", "version": _APP_VERSION}

# =========================================================================
# PUBLIC ENDPOINTS (read-only, no auth required)
# =========================================================================

@app.get("/api/hosts")
async def get_hosts(background_tasks: BackgroundTasks, request: Request,
                    owner: str = None,
                    pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get hosts with update status, scoped by role when authenticated."""
    user = await get_current_user(request, pool)
    uid = owner_id_or_param(user, owner) if user else None

    if uid is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT h.*, u.username AS owner_username FROM hosts h "
                "LEFT JOIN users u ON h.created_by = u.id "
                "WHERE h.created_by = $1 ORDER BY h.hostname", uid
            )
            hosts = [dict(r) for r in rows]
    else:
        # For full_admin unfiltered, add owner_username
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT h.*, u.username AS owner_username FROM hosts h "
                "LEFT JOIN users u ON h.created_by = u.id "
                "ORDER BY h.hostname"
            )
            hosts = [dict(r) for r in rows]

    # Auto-trigger a check if data looks stale and nothing is running
    if hosts and not _ansible_check_lock.locked() and not _ansible_patch_running:
        try:
            pool = db.pool
            async with pool.acquire() as conn:
                interval = await conn.fetchval(
                    "SELECT value FROM settings WHERE key = 'refresh_interval'"
                )
                interval_secs = max(30, int(interval)) if interval else 300
            # Check if the most recent last_checked across all hosts is older
            # than 2× the refresh interval (i.e. at least one full cycle was missed)
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            threshold = now - timedelta(seconds=interval_secs * 2)
            newest_check = None
            for h in hosts:
                lc = h.get('last_checked')
                if lc is not None:
                    # Ensure timezone-aware comparison
                    if lc.tzinfo is None:
                        lc = lc.replace(tzinfo=timezone.utc)
                    if newest_check is None or lc > newest_check:
                        newest_check = lc
            if newest_check is None or newest_check < threshold:
                logger.info(f"Host data is stale (newest_check={newest_check}) — auto-triggering check")
                background_tasks.add_task(run_ansible_check_task)
        except Exception as e:
            logger.debug(f"Auto-check trigger skipped: {e}")

    return hosts

@app.get("/api/hosts/{hostname}")
async def get_host(hostname: str, request: Request,
                   pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get details for a specific host (ownership-scoped when authenticated)"""
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    user = await get_current_user(request, pool)
    if user:
        async with pool.acquire() as conn:
            if not await verify_host_ownership_by_hostname(conn, user, hostname):
                raise HTTPException(status_code=403, detail="Access denied to this host")
    return host

@app.get("/api/hosts/{hostname}/packages")
async def get_host_packages(hostname: str, request: Request,
                            pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get pending updates for a specific host (ownership-scoped)"""
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    user = await get_current_user(request, pool)
    if user:
        async with pool.acquire() as conn:
            if not await verify_host_ownership_by_hostname(conn, user, hostname):
                raise HTTPException(status_code=403, detail="Access denied to this host")
    packages = await db.get_packages_for_host(host['id'])
    return packages

@app.get("/api/stats")
async def get_stats(request: Request, owner: str = None,
                    pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get summary statistics, scoped by role when authenticated."""
    user = await get_current_user(request, pool)
    uid = owner_id_or_param(user, owner) if user else None
    if uid is not None:
        async with pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total_hosts,
                    COUNT(*) FILTER (WHERE status = 'up-to-date') as up_to_date,
                    COUNT(*) FILTER (WHERE status = 'updates-available') as need_updates,
                    COUNT(*) FILTER (WHERE status = 'unreachable') as unreachable,
                    COALESCE(SUM(total_updates), 0) as total_pending_updates
                FROM hosts WHERE created_by = $1
            """, uid)
            return {
                "total_hosts": result['total_hosts'],
                "up_to_date": result['up_to_date'],
                "need_updates": result['need_updates'],
                "unreachable": result['unreachable'],
                "total_pending_updates": result['total_pending_updates']
            }
    stats = await db.get_stats()
    return stats

@app.get("/api/stats/charts")
async def get_chart_data(request: Request, owner: str = None,
                         pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Get dashboard chart data: OS distribution, update types, patch activity. Scoped by role."""
    pool = db.pool
    user = await get_current_user(request, pool_dep)
    uid = owner_id_or_param(user, owner) if user else None
    
    # OS Distribution
    if uid is not None:
        os_rows = await pool.fetch("""
            SELECT COALESCE(os_family, 'Unknown') as os, COUNT(*) as count
            FROM hosts WHERE created_by = $1 GROUP BY os_family ORDER BY count DESC
        """, uid)
    else:
        os_rows = await pool.fetch("""
            SELECT COALESCE(os_family, 'Unknown') as os, COUNT(*) as count
            FROM hosts GROUP BY os_family ORDER BY count DESC
        """)
    os_distribution = [{"os": r['os'], "count": r['count']} for r in os_rows]
    
    # Update Types
    if uid is not None:
        pkg_rows = await pool.fetch("""
            SELECT COALESCE(p.update_type, 'unknown') as type, COUNT(*) as count
            FROM packages p
            JOIN hosts h ON p.host_id = h.id
            WHERE h.created_by = $1
            GROUP BY p.update_type ORDER BY count DESC
        """, uid)
    else:
        pkg_rows = await pool.fetch("""
            SELECT COALESCE(p.update_type, 'unknown') as type, COUNT(*) as count
            FROM packages p
            JOIN hosts h ON p.host_id = h.id
            GROUP BY p.update_type ORDER BY count DESC
        """)
    update_types = [{"type": r['type'], "count": r['count']} for r in pkg_rows]
    
    # Patch Activity - last 7 days (always returns all 7 days even with no data)
    # Also returns per-OS breakdown so the frontend can draw OS-colored stacked bars
    activity = []
    try:
        if uid is not None:
            activity_rows = await pool.fetch("""
                SELECT
                    gs.day::date                                                   AS day,
                    COALESCE(SUM(COALESCE(array_length(ph.packages_updated, 1), 0))
                        FILTER (WHERE ph.success = TRUE),  0)::int                 AS patched,
                    COALESCE(COUNT(ph.id) FILTER (WHERE ph.success = FALSE
                                                     OR ph.success IS NULL),  0)   AS failed
                FROM generate_series(
                         CURRENT_DATE - INTERVAL '6 days',
                         CURRENT_DATE,
                         INTERVAL '1 day'
                     ) AS gs(day)
                LEFT JOIN patch_history ph
                       ON DATE(ph.execution_time AT TIME ZONE 'UTC') = gs.day::date
                LEFT JOIN hosts h ON ph.host_id = h.id AND h.created_by = $1
                WHERE ph.id IS NULL OR h.id IS NOT NULL
                GROUP BY gs.day
                ORDER BY gs.day
            """, uid)
        else:
            activity_rows = await pool.fetch("""
                SELECT
                    gs.day::date                                                   AS day,
                    COALESCE(SUM(COALESCE(array_length(ph.packages_updated, 1), 0))
                        FILTER (WHERE ph.success = TRUE),  0)::int                 AS patched,
                    COALESCE(COUNT(ph.id) FILTER (WHERE ph.success = FALSE
                                                     OR ph.success IS NULL),  0)   AS failed
                FROM generate_series(
                         CURRENT_DATE - INTERVAL '6 days',
                         CURRENT_DATE,
                         INTERVAL '1 day'
                     ) AS gs(day)
                LEFT JOIN patch_history ph
                       ON DATE(ph.execution_time AT TIME ZONE 'UTC') = gs.day::date
                GROUP BY gs.day
                ORDER BY gs.day
            """)

        # Per-OS successful patch counts per day
        if uid is not None:
            os_rows = await pool.fetch("""
                SELECT
                    DATE(ph.execution_time AT TIME ZONE 'UTC') AS day,
                    COALESCE(h.os_family, 'Unknown')           AS os_family,
                    SUM(COALESCE(array_length(ph.packages_updated, 1), 0))::int AS count
                FROM patch_history ph
                LEFT JOIN hosts h ON ph.host_id = h.id
                WHERE ph.success = TRUE
                  AND ph.execution_time >= CURRENT_DATE - INTERVAL '6 days'
                  AND h.created_by = $1
                GROUP BY DATE(ph.execution_time AT TIME ZONE 'UTC'), h.os_family
                ORDER BY day
            """, uid)
        else:
            os_rows = await pool.fetch("""
                SELECT
                    DATE(ph.execution_time AT TIME ZONE 'UTC') AS day,
                    COALESCE(h.os_family, 'Unknown')           AS os_family,
                    SUM(COALESCE(array_length(ph.packages_updated, 1), 0))::int AS count
                FROM patch_history ph
                LEFT JOIN hosts h ON ph.host_id = h.id
                WHERE ph.success = TRUE
                  AND ph.execution_time >= CURRENT_DATE - INTERVAL '6 days'
                GROUP BY DATE(ph.execution_time AT TIME ZONE 'UTC'), h.os_family
                ORDER BY day
            """)

        # Index os breakdown by day string
        os_by_day: dict = {}
        for r in os_rows:
            day_str = str(r['day'])
            if day_str not in os_by_day:
                os_by_day[day_str] = {}
            os_by_day[day_str][r['os_family']] = r['count']

        activity = [
            {
                "day":    str(r['day']),
                "patched": r['patched'],
                "failed":  r['failed'],
                "by_os":   os_by_day.get(str(r['day']), {})
            }
            for r in activity_rows
        ]
    except Exception as e:
        print(f"[WARN] patch_activity query failed: {e}")
    
    return {
        "os_distribution": os_distribution,
        "update_types": update_types,
        "patch_activity": activity
    }


@app.get("/api/stats/sidebar")
async def get_sidebar_stats(request: Request, owner: str = None,
                            pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Get sidebar-specific stats: load average, uptime, counts for badges. Scoped by role."""
    pool = db.pool
    user = await get_current_user(request, pool_dep)
    uid = owner_id_or_param(user, owner) if user else None
    
    # System load average
    try:
        load_avg = psutil.getloadavg()
        load_1, load_5, load_15 = round(load_avg[0], 2), round(load_avg[1], 2), round(load_avg[2], 2)
    except Exception:
        load_1, load_5, load_15 = 0, 0, 0
    
    # App uptime
    uptime_seconds = int(time.time() - APP_START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    uptime_str = f"{days}d {hours}h {minutes}m"
    
    if uid is not None:
        # Scoped counts for admin role
        host_count = await pool.fetchval(
            "SELECT COUNT(*) FROM hosts WHERE created_by = $1", uid) or 0
        pkg_count = await pool.fetchval(
            "SELECT COUNT(*) FROM packages p JOIN hosts h ON p.host_id = h.id WHERE h.created_by = $1", uid) or 0
        history_count = 0
        try:
            history_count = await pool.fetchval(
                "SELECT COALESCE(SUM(COALESCE(array_length(ph.packages_updated, 1), 0)), 0)::int "
                "FROM patch_history ph JOIN hosts h ON ph.host_id = h.id "
                "WHERE ph.execution_time > NOW() - INTERVAL '7 days' AND ph.success = TRUE AND h.created_by = $1",
                uid) or 0
        except Exception:
            pass
        alert_count = 0
        try:
            alert_count = await pool.fetchval("""
                SELECT (
                    SELECT COUNT(*) FROM hosts 
                    WHERE (status = 'unreachable' OR reboot_required = TRUE) AND created_by = $1
                ) + (
                    SELECT COUNT(DISTINCT h.id) FROM hosts h
                    JOIN packages p ON h.id = p.host_id
                    WHERE p.update_type = 'macos-system' AND h.created_by = $1
                )
            """, uid) or 0
        except Exception:
            pass
    else:
        # Full view (full_admin, viewer, or unauthenticated)
        host_count = await pool.fetchval("SELECT COUNT(*) FROM hosts") or 0
        pkg_count = await pool.fetchval("SELECT COUNT(*) FROM packages") or 0
        history_count = 0
        try:
            history_count = await pool.fetchval(
                "SELECT COALESCE(SUM(COALESCE(array_length(packages_updated, 1), 0)), 0)::int "
                "FROM patch_history WHERE execution_time > NOW() - INTERVAL '7 days' AND success = TRUE"
            ) or 0
        except Exception:
            pass
        alert_count = 0
        try:
            alert_count = await pool.fetchval("""
                SELECT (
                    SELECT COUNT(*) FROM hosts 
                    WHERE status = 'unreachable' OR reboot_required = TRUE
                ) + (
                    SELECT COUNT(DISTINCT h.id) FROM hosts h
                    JOIN packages p ON h.id = p.host_id
                    WHERE p.update_type = 'macos-system'
                )
            """) or 0
        except Exception:
            pass
    
    return {
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "uptime": uptime_str,
        "host_count": host_count,
        "package_count": pkg_count,
        "history_count": history_count,
        "alert_count": alert_count
    }


@app.get("/api/patch-history")
async def get_patch_history(limit: int = 50, owner: str = None,
                            request: Request = None,
                            pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Get patch history records, scoped by role."""
    pool = db.pool
    user = await get_current_user(request, pool_dep) if request else None
    uid = owner_id_or_param(user, owner) if user else None
    async with pool.acquire() as conn:
        if uid is not None:
            rows = await conn.fetch("""
                SELECT ph.id, ph.host_id, h.hostname,
                       ph.success, ph.packages_updated,
                       ph.execution_time, ph.duration_seconds, ph.error_message,
                       ph.output
                FROM patch_history ph
                LEFT JOIN hosts h ON ph.host_id = h.id
                WHERE h.created_by = $1
                ORDER BY ph.execution_time DESC
                LIMIT $2
            """, uid, limit)
        else:
            rows = await conn.fetch("""
                SELECT ph.id, ph.host_id, h.hostname,
                       ph.success, ph.packages_updated,
                       ph.execution_time, ph.duration_seconds, ph.error_message,
                       ph.output
                FROM patch_history ph
                LEFT JOIN hosts h ON ph.host_id = h.id
                ORDER BY ph.execution_time DESC
                LIMIT $1
            """, limit)
        result = []
        for r in rows:
            d = dict(r)
            d["status"] = "success" if d.get("success") else "failed"
            d["created_at"] = str(d.get("execution_time", ""))
            stored_pkgs = d.get("packages_updated") or []
            # Backfill count from output if packages_updated was stored empty
            if not stored_pkgs and d.get("output"):
                hostname = d.get("hostname", "")
                stored_pkgs = _extract_packages_updated(d["output"], hostname)
            d["packages_updated"] = len(stored_pkgs)
            d["execution_time"] = d.get("duration_seconds", 0)
            d["output"] = d.get("output") or ""
            result.append(d)
        return result


@app.get("/api/patch-history/host/{host_id}")
async def get_patch_history_by_host(host_id: str, limit: int = 20,
                                    request: Request = None,
                                    pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Get patch history for a specific host (ownership-scoped)"""
    pool = db.pool
    user = await get_current_user(request, pool_dep) if request else None
    if user:
        async with pool.acquire() as conn:
            from rbac import verify_host_ownership
            try:
                host_uuid = uuid.UUID(host_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid host ID")
            if not await verify_host_ownership(conn, user, host_uuid):
                raise HTTPException(status_code=403, detail="Access denied to this host")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ph.id, ph.host_id, h.hostname,
                   ph.success, ph.packages_updated,
                   ph.execution_time, ph.duration_seconds, ph.error_message,
                   ph.output
            FROM patch_history ph
            LEFT JOIN hosts h ON ph.host_id = h.id
            WHERE ph.host_id = $1
            ORDER BY ph.execution_time DESC
            LIMIT $2
        """, host_id, limit)
        result = []
        for r in rows:
            d = dict(r)
            d["status"] = "success" if d.get("success") else "failed"
            d["created_at"] = str(d.get("execution_time", ""))
            stored_pkgs = d.get("packages_updated") or []
            # Backfill from output if stored empty (legacy records before fix)
            if not stored_pkgs and d.get("output"):
                hostname = d.get("hostname", "")
                stored_pkgs = _extract_packages_updated(d["output"], hostname)
            d["packages_updated"] = stored_pkgs
            d["duration_seconds"] = d.get("duration_seconds", 0)
            d["output"] = d.get("output") or ""
            result.append(d)
        return result


@app.get("/api/backend-logs")
async def get_backend_logs(limit: int = 200, level: str = "all",
                           user: dict = Depends(require_full_admin)):
    """Return recent backend log lines from in-memory ring buffer (full_admin only)"""
    entries = list(_LOG_RING_BUFFER)
    if level != "all":
        entries = [e for e in entries if e["lvl"] == level]
    return entries[-limit:]


@app.get("/api/debug")
async def get_debug_mode(user: dict = Depends(require_full_admin),
                         pool: asyncpg.Pool = Depends(get_db_pool)):
    """Return current debug mode state."""
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT value FROM settings WHERE key = 'debug_mode'")
    enabled = (val or 'false').lower() == 'true'
    return {"debug_mode": enabled}


@app.put("/api/debug")
async def set_debug_mode(request: Request,
                         user: dict = Depends(require_full_admin),
                         pool: asyncpg.Pool = Depends(get_db_pool)):
    """Toggle debug logging at runtime. Persists to settings table."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))

    # Persist to DB
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES ('debug_mode', $1, NOW())
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
        """, 'true' if enabled else 'false')

    # Apply at runtime — toggle log levels on all patchpilot loggers
    target_level = logging.DEBUG if enabled else logging.INFO
    logging.getLogger().setLevel(target_level)
    # Also set our specific module loggers
    for name in ('patchpilot', 'patchpilot.updates', __name__):
        logging.getLogger(name).setLevel(logging.DEBUG if enabled else logging.DEBUG)
    # Third-party loggers stay at INFO unless debug is on
    for name in ('uvicorn', 'asyncpg', 'httpx', 'paramiko'):
        logging.getLogger(name).setLevel(target_level)

    state = "enabled" if enabled else "disabled"
    logger.info(f"Debug mode {state} by {user['username']}")
    print(f"[DEBUG] Debug mode {state} — root logger level set to {logging.getLevelName(target_level)}")

    return {"debug_mode": enabled, "message": f"Debug logging {state}"}


@app.get("/api/alerts")
async def get_alerts(request: Request, owner: str = None,
                     pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Get current alerts, scoped by role."""
    pool = db.pool
    user = await get_current_user(request, pool_dep)
    uid = owner_id_or_param(user, owner) if user else None
    alerts = []
    try:
        async with pool.acquire() as conn:
            if uid is not None:
                rows = await conn.fetch("""
                    SELECT hostname, ip_address, status, reboot_required, last_checked
                    FROM hosts
                    WHERE (status = 'unreachable' OR reboot_required = TRUE)
                      AND created_by = $1
                    ORDER BY status, hostname
                """, uid)
            else:
                rows = await conn.fetch("""
                    SELECT hostname, ip_address, status, reboot_required, last_checked
                    FROM hosts
                    WHERE status = 'unreachable' OR reboot_required = TRUE
                    ORDER BY status, hostname
                """)
            for r in rows:
                if r['status'] == 'unreachable':
                    alerts.append({
                        "severity": "error",
                        "type": "unreachable",
                        "hostname": r['hostname'],
                        "message": f"Host {r['hostname']} is unreachable",
                        "last_checked": str(r['last_checked']) if r['last_checked'] else None
                    })
                elif r['reboot_required']:
                    alerts.append({
                        "severity": "warning",
                        "type": "reboot_required",
                        "hostname": r['hostname'],
                        "message": f"Host {r['hostname']} requires a reboot",
                        "last_checked": str(r['last_checked']) if r['last_checked'] else None
                    })
            # macOS system updates: detected during check but not auto-installed
            if uid is not None:
                macos_rows = await conn.fetch("""
                    SELECT DISTINCT h.hostname, h.last_checked
                    FROM hosts h
                    JOIN packages p ON h.id = p.host_id
                    WHERE p.update_type = 'macos-system' AND h.created_by = $1
                """, uid)
            else:
                macos_rows = await conn.fetch("""
                    SELECT DISTINCT h.hostname, h.last_checked
                    FROM hosts h
                    JOIN packages p ON h.id = p.host_id
                    WHERE p.update_type = 'macos-system'
                """)
            for r in macos_rows:
                alerts.append({
                    "severity": "info",
                    "type": "macos_system_update",
                    "hostname": r['hostname'],
                    "message": f"macOS system update available on {r['hostname']} — install via System Settings and reboot",
                    "last_checked": str(r['last_checked']) if r['last_checked'] else None
                })
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
    return alerts


# =========================================================================
# PROTECTED ENDPOINTS (require authentication)
# =========================================================================

@app.post("/api/hosts/{hostname}/dismiss-reboot")
async def dismiss_reboot_alert(hostname: str,
                               user: dict = Depends(require_write),
                               pool: asyncpg.Pool = Depends(get_db_pool)):
    """Clear the reboot_required flag for a host (ownership-scoped, write-only)."""
    async with pool.acquire() as conn:
        if not await verify_host_ownership_by_hostname(conn, user, hostname):
            raise HTTPException(status_code=403, detail="Access denied to this host")
    pool2 = db.pool
    async with pool2.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE hosts SET reboot_required = FALSE WHERE hostname = $1 RETURNING id",
            hostname
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    return {"message": f"Reboot alert dismissed for {hostname}"}


@app.post("/api/check")
async def trigger_check(background_tasks: BackgroundTasks,
                        user: dict = Depends(require_write)):
    """Trigger an immediate Ansible check (PROTECTED).

    If a check is already running, we schedule one to run immediately after
    the lock releases rather than silently dropping the request.  Without
    this, pressing REFRESH while the periodic background check happens to be
    running causes the entire request to be discarded — the frontend polls
    for 3 minutes and nothing happens until the next periodic timer fires.
    """
    if _ansible_check_lock.locked() or _ansible_patch_running:
        # Already busy — queue a follow-up run instead of silently dropping
        async def _run_after_current():
            # Wait for the current run to finish (poll up to 5 min)
            for _ in range(300):
                if not _ansible_check_lock.locked() and not _ansible_patch_running:
                    break
                await asyncio.sleep(1)
            await run_ansible_check_task()
        background_tasks.add_task(_run_after_current)
        return {"message": "Check queued (another check is running)", "status": "queued"}

    background_tasks.add_task(run_ansible_check_task)
    return {"message": "Check initiated", "status": "running"}

@app.post("/api/check/{hostname}")
async def trigger_single_host_check(hostname: str, background_tasks: BackgroundTasks,
                                    user: dict = Depends(require_write),
                                    pool: asyncpg.Pool = Depends(get_db_pool)):
    """Trigger an immediate Ansible check for a single host (ownership-scoped, write-only)"""
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    async with pool.acquire() as conn:
        if not await verify_host_ownership_by_hostname(conn, user, hostname):
            raise HTTPException(status_code=403, detail="Access denied to this host")
    if _ansible_check_lock.locked() or _ansible_patch_running:
        async def _run_after_current(h=[hostname]):
            for _ in range(300):
                if not _ansible_check_lock.locked() and not _ansible_patch_running:
                    break
                await asyncio.sleep(1)
            await run_ansible_check_task(h)
        background_tasks.add_task(_run_after_current)
        return {"message": f"Check queued for {hostname} (another check is running)", "status": "queued"}
    background_tasks.add_task(run_ansible_check_task, [hostname])
    return {"message": f"Check initiated for {hostname}", "status": "running"}

@app.get("/api/patch/status")
async def get_patch_status():
    """Return whether a patch run is currently in progress (PUBLIC - read only).
    Used by the frontend to recover from WebSocket disconnects during long
    patch operations (e.g. large App Store downloads).
    Also useful for debugging stuck flags from inside the container."""
    return {
        "running": _ansible_patch_running or _ansible_check_lock.locked(),
        "patch_running": _ansible_patch_running,
        "patch_running_seconds": int(time.monotonic() - _ansible_patch_running_since) if _ansible_patch_running_since else None,
        "check_running": _ansible_check_lock.locked(),
        "check_running_seconds": int(time.monotonic() - _ansible_check_lock_since) if _ansible_check_lock_since else None,
    }

@app.post("/api/patch")
async def trigger_patch(patch_request: PatchRequest, background_tasks: BackgroundTasks,
                        request: Request,
                        user: dict = Depends(require_write),
                        pool_dep: asyncpg.Pool = Depends(get_db_pool)):
    """Trigger patching for specific hosts (ownership-scoped, write-only)"""
    if not patch_request.hostnames:
        raise HTTPException(status_code=400, detail="No hostnames provided")
    
    async with pool_dep.acquire() as conn:
        for hostname in patch_request.hostnames:
            host = await db.get_host_by_hostname(hostname)
            if not host:
                raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
            if not await verify_host_ownership_by_hostname(conn, user, hostname):
                raise HTTPException(status_code=403, detail=f"Access denied to host {hostname}")
    
    # Audit log the patch operation
    pool = db.pool
    await log_audit(
        pool, str(user['id']), user['username'], "patch_initiated",
        resource_type="hosts", resource_id=",".join(patch_request.hostnames),
        details={"host_count": len(patch_request.hostnames)},
        ip_address=request.client.host if request.client else None
    )
    
    background_tasks.add_task(
        run_ansible_patch_task, 
        patch_request.hostnames,
        patch_request.become_password
    )
    return {
        "message": "Patch initiated",
        "status": "running",
        "hosts": patch_request.hostnames
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
