# PatchPilot — Backup & Restore Feature

> **Note:** Backup & restore features require an active PatchPilot license. Trial users can view the backup list but cannot create, download, upload, restore, or delete backups. Purchase a license at [getpatchpilot.app](https://getpatchpilot.app).

## What's Included

| File | Purpose |
|------|---------|
| `backend/backup_restore.py` | FastAPI router + full backup/restore logic |
| `backend/app_integration_patch.py` | Annotated snippets showing how to wire into `app.py` |
| `backend/requirements.txt` | Updated requirements (no new pip packages needed) |
| `frontend/backup_restore_tab.html` | Drop-in HTML+CSS+JS tab for the settings page |
| `Dockerfile` | Updated to install `postgresql-client` tools |
| `docker-compose.yml` | Updated with `backups` volume + optional docker.sock mount |
| `k8s/backup-restore-additions.yaml` | PVC, env var patches, and daily CronJob for k3s |
| `scripts/patchpilot-backup.sh` | Host-side CLI for manual/cron backup operations |

---

## Integration Steps

### 1. Update Dockerfile
Replace your existing `Dockerfile` with the provided one. The key addition is `postgresql-client` which supplies `pg_dump`, `pg_restore`, `psql`, `dropdb`, and `createdb`.

### 2. Update docker-compose.yml
Merge the `backups` volume and the new environment variables into your compose file. The `POSTGRES_CONTAINER_NAME` should match your postgres container's `container_name`.

### 3. Wire backup_restore.py into app.py
Open `backend/app_integration_patch.py` and follow the annotated instructions — there are 4 short additions:
```python
# 1. Import at top
from backup_restore import router as backup_router, set_pool, maintenance_mode

# 2. Register router  
app.include_router(backup_router)

# 3. Share pool after creation
set_pool(pool)

# 4. Add maintenance middleware (optional but recommended)
```

### 4. Add the frontend tab
In your `frontend/index.html` (or settings page):
- Paste the **TAB BUTTON** snippet into your settings nav
- Paste the **TAB PANEL** snippet into your settings content area
- Paste the `<style>` block into `styles.css`
- Paste the `<script>` block into `app.js` (or before `</body>`)

### 5. Rebuild and deploy
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## How It Works

### Backup Flow

```
User clicks "Create Backup"
        │
        ▼
Backend sets maintenance_mode = True
(new patch/write requests → 503)
        │
        ▼
pg_terminate_backend() clears all active
connections from the application pool
(PostgreSQL is still RUNNING — pg_dump needs it)
        │
        ▼
pg_dump --format=custom runs against quiescent DB
(custom format: compressed + supports selective restore)
        │
        ▼
settings_export.json written (human-readable settings)
        │
        ▼
/ansible/* files copied into staging
        │
        ▼
Optional: PATCHPILOT_ENCRYPTION_KEY written to
encryption_key.json (user-controlled toggle)
        │
        ▼
backup_metadata.json written
        │
        ▼
All staged files → patchpilot_backup_YYYYMMDD_HHMMSS.tar.gz
        │
        ▼
Retention policy enforced (keep last N backups)
        │
        ▼
maintenance_mode = False
```

### Restore Flow

```
User selects backup → clicks "Restore Now"
        │
        ▼
Backend sets maintenance_mode = True
        │
        ▼
All active DB connections terminated via
pg_terminate_backend()
        │
        ▼
asyncpg connection pool closed
        │
        ▼
dropdb patchpilot (via admin connection to postgres DB)
createdb patchpilot
        │
        ▼
pg_restore --exit-on-error populates fresh database
        │
        ▼
asyncpg pool rebuilt (reconnects to restored DB)
        │
        ▼
/ansible/ files restored from backup
        │
        ▼
Post-restore verification (row counts)
        │
        ▼
maintenance_mode = False
Frontend polls status → auto-reloads page
```

### Why Not Literally Stop Postgres?

`pg_dump` requires a live PostgreSQL connection — you can't dump a stopped database. The approach used here is equivalent and actually safer:

- **For backup**: `pg_terminate_backend()` evicts all application connections, giving `pg_dump` a clean, quiescent database. `pg_dump` uses repeatable-read isolation internally so it's fully consistent.
- **For restore**: We drop and recreate the entire database, which atomically replaces all data. No rows from the old state survive.
- **Optional hard stop**: If you mount `/var/run/docker.sock`, the module can call `docker stop patchpilot-postgres-1` before a restore and `docker start` after. See `docker-compose.yml` comments.

---

## Backup Archive Contents

```
patchpilot_backup_20260220_020000.tar.gz
└── patchpilot_backup_20260220_020000/
    ├── patchpilot.dump          ← pg_dump custom format (compressed)
    ├── backup_metadata.json     ← timestamps, versions, restore command
    ├── settings_export.json     ← human-readable settings + host list
    ├── ansible/
    │   ├── hosts                ← Ansible inventory
    │   └── check-os-updates.yml ← Ansible playbook(s)
    └── encryption_key.json      ← (only if "Include encryption key" toggled ON)
```

---

## CLI Usage

The host-side CLI script (`scripts/patchpilot-backup.sh`) is useful for cron jobs and disaster recovery:

```bash
# Make executable
chmod +x scripts/patchpilot-backup.sh

# List backups
./scripts/patchpilot-backup.sh list

# Create backup
./scripts/patchpilot-backup.sh backup "Pre-upgrade snapshot"

# Download a backup to local disk
./scripts/patchpilot-backup.sh download patchpilot_backup_20260220_020000.tar.gz

# Upload an archive back to server
./scripts/patchpilot-backup.sh upload ./backups/patchpilot_backup_20260220_020000.tar.gz

# Restore (prompts for confirmation)
./scripts/patchpilot-backup.sh restore patchpilot_backup_20260220_020000.tar.gz
```

### Cron Example (daily 2 AM backup on the host)
```cron
0 2 * * * PATCHPILOT_URL=http://localhost:8000 /opt/patchpilot/scripts/patchpilot-backup.sh backup "Nightly cron" >> /var/log/patchpilot-backup.log 2>&1
```

---

## Kubernetes Notes

The `k8s/backup-restore-additions.yaml` includes:
- A **PVC** (`patchpilot-backups-pvc`) for persistent backup storage
- **Environment variable patches** to add to your backend Deployment
- A **CronJob** that triggers the backup API daily at 2 AM

For k8s restores, scale the backend to 0 first for cleanest results:
```bash
kubectl scale deployment patchpilot-backend -n patchpilot --replicas=0
# ... run restore via API or exec ...
kubectl scale deployment patchpilot-backend -n patchpilot --replicas=1
```

---

## Security Notes

- The `backups` volume is **not** publicly accessible — only through the `/api/backup/download/<filename>` endpoint
- Backup filenames are sanitized server-side to prevent path traversal
- If you include the encryption key in a backup, treat that archive like a secret — it can decrypt all stored SSH credentials
- The Docker socket mount (optional) gives the backend container Docker daemon access; only enable it if your API endpoints are not publicly exposed
- Consider restricting the `/api/backup/*` endpoints with authentication middleware if your PatchPilot instance is network-accessible
