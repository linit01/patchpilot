# Changelog

All notable changes to PatchPilot will be documented in this file.

---

## [0.9.7-alpha] — 2026-03-07

### Fixed
- **`setup_api.py`: missing `await` on `get_db_pool()`** — Two call sites in the setup-restore
  flow called `get_db_pool()` synchronously (without `await`), returning the coroutine object
  instead of the actual pool.  This caused `AttributeError` or silent failures when restoring
  from a backup during first-run setup.
- **Ansible parser: dashboard / host-detail mismatch on update counts** — When Ansible returned
  a status message claiming N updates but zero `PACKAGE:` lines were parseable, the parser
  kept the stale `total_updates` from the status message while the packages table had 0 rows.
  The dashboard showed "65 updates" but host details showed nothing.  The parser now resets
  `total_updates` to 0 when no package details are parsed and adjusts `status` accordingly,
  with warnings logged for investigation.
- **Unchecked hosts keep stale status after Ansible check** — If Ansible aborted early or a
  host was unreachable before `ignore_unreachable` could kick in, hosts that were never
  evaluated kept their old status.  `run_ansible_check_task()` now compares the expected host
  set against what Ansible actually returned and marks the gap as `unreachable` with
  `total_updates=0`.
- **`_ansible_check_lock` / `_ansible_patch_running` stuck forever** — A hanging Ansible process
  or unhandled exception could leave these flags locked permanently, blocking all future checks
  and patches until the container restarted.  Both now have monotonic-clock timeouts
  (10 min for check lock, 30 min for patch flag) with auto-clear and warning logs.
- **Periodic check loop dies silently on exception** — An unhandled exception in
  `run_ansible_check_task()` would kill the `periodic_ansible_check` coroutine permanently.
  The loop now wraps each cycle in try/except with error logging.
- **Scheduler fires before initial host check completes** — On startup (or after restore +
  restart), the scheduler could evaluate schedules before the first Ansible check populated
  host status and `total_updates`.  With stale or zero data it would either skip hosts that
  need patching or patch hosts that are already current.  The scheduler now waits on an
  `_initial_check_done` asyncio Event (120s ceiling) before its first evaluation.
- **Scheduled patches not recorded in `patch_history`** — Manual patches wrote history but
  scheduled patches did not.  `run_scheduled_patch()` now records `patch_history` rows for
  each host (success/fail, packages updated, duration, error message, raw output).
- **Scheduler re-patches already-current hosts on every tick** — The `already_ran_today` gate
  was binary: either all hosts or just the retry list.  If a schedule window was still open
  and no new updates appeared, every host got re-patched every 60 seconds.  The scheduler now
  queries each host's current `status` and `total_updates` and only targets hosts that
  actually need patching (updates > 0, not offline/unreachable) plus any explicit retry hosts.
- **Restore leaves dead connection pools** — After a DB drop/recreate during restore, the old
  `asyncpg.Pool` and `DatabaseClient.pool` objects held dead connections.  Endpoints using
  `Depends(get_db_pool)` would fail until a container restart.  `_rebuild_pool()` in
  `backup_restore.py` now rebuilds both pools, and `dependencies.py` exposes `set_pool()` /
  `rebuild_pool()` so all pool references stay in sync.
- **Setup-restore leaves pools dead** — Same issue as above but in the first-run setup flow.
  `setup_api.py` now calls `rebuild_pool()` after pg_restore and syncs the backup_restore
  module's pool reference.
- **Post-restore dashboard shows stale data until next periodic tick** — After a successful
  restore (without self-restart), the dashboard displayed whatever was in the restored DB
  without an Ansible check to verify current host state.  `backup_restore.py` now accepts a
  `set_post_restore_callback()` wired by `app.py` to trigger an immediate Ansible check.
- **`/api/hosts` returns stale data after restore or long downtime** — If all hosts have
  `last_checked` older than 2× the refresh interval (i.e. at least one full check cycle was
  missed), the endpoint now auto-triggers a background Ansible check so the dashboard
  self-heals without requiring a manual refresh.
- **Frontend: fixed countdown timer on setup/restore completion** — Replaced hardcoded 5s/18s
  redirect timers with a `/health` polling loop that enables the "Sign In" button only after
  the backend is confirmed ready, preventing login attempts against a restarting backend.
- **K8s uninstall: cleanup Job requires `privileged: true`** — The busybox Job used
  `securityContext.privileged: true` which is blocked by many PodSecurityPolicies.  Changed to
  `runAsUser: 0` only — sufficient for hostPath file deletion.
- **K8s uninstall: hardcoded `/app-data` path** — All cleanup references assumed the default
  `/app-data` directory.  The uninstall now discovers the actual data directory from PV
  hostPath specs (falling back to `PATCHPILOT_DATA_DIR` env var, then `/app-data`).
- **K8s uninstall: cleanup Job races with postgres volume mount** — The cleanup Job could run
  while postgres still held its hostPath mount, causing `rm -rf` to fail or produce incomplete
  cleanup.  Postgres and frontend are now scaled to 0 replicas (and waited on) before the
  cleanup Job starts.  The backend stays alive to orchestrate the remaining steps.
- **Backup filename glob misses new naming format** — Retention, listing, download, upload,
  delete, and health endpoints all used `glob("patchpilot_backup_*.tar.gz")`.  Centralized
  into `_is_backup_file()` / `_list_backup_archives()` helpers that recognize both legacy
  (`patchpilot_backup_*`) and new (`patchpilot_*`) naming prefixes with `.tgz` or `.tar.gz`.
- **`/api/patch/status` requires auth** — Made this endpoint public (read-only) so the frontend
  can recover from WebSocket disconnects during long patch operations without requiring a
  re-auth handshake.
- **Frontend: nginx default page shows dashboard instead of login** — Changed `index` and
  `try_files` fallback from `index.html` to `login.html` in the k8s frontend nginx config.
- **`dependencies.get_db_pool` import scattered through `app.py`** — Multiple callsites did
  `from dependencies import get_db_pool` inline.  Replaced with direct `db.pool` access to
  avoid redundant imports and keep a single source of truth.
- **macOS check fails with `'timeout_bin' is undefined` when mas enabled** — The "Warn if no
  timeout binary" task in `check-os-updates.yml` referenced `timeout_bin.stdout` instead of
  `timeout_bin_path`.  With `mas_enabled=false` the bug was hidden by short-circuit evaluation.

### Added
- **macOS system update detection and configurable install** — New `macos_system_updates_enabled`
  setting (default `false`).  When disabled, PatchPilot detects available macOS system updates
  and reports them as `macos-system` package type but does not attempt to install them
  (softwareupdate over SSH is unreliable on newer macOS).  When enabled, non-control nodes
  get `softwareupdate -iaR` (install all + reboot) and control nodes get download-only with a
  notification to install via System Settings.
- **macOS system update alerts** — Dashboard alerts now include an `info`-severity alert type
  for hosts with detected macOS system updates, with blue styling in the frontend.  Stats
  alert count includes these hosts.
- **Backup configuration panel** (Settings → Backup & Restore) — New UI card to view/change
  backup storage type (local / NFS), NFS server/share, and retention count.  Displays a hint
  when the selected config requires a volume change (Docker volume recreate or k8s PV update).
  Backup health endpoint exposes `env_backup_storage_type`, `env_nfs_server`, `env_nfs_share`,
  `env_backup_retain_count`, and `install_mode` so the frontend can detect mismatches.
- **`apt-get update` before patching** — Debian/Ubuntu patch runs now refresh the apt cache
  before applying updates, preventing stale package list errors.
- **Ansible `--forks 5` for check, `--forks 1` for patch** — Check playbook runs with 5 forks
  for faster parallel host checking.  Patch playbook uses 1 fork (serial) to avoid SSH slot
  exhaustion and allow per-host progress streaming.
- **Ansible explicit fact gathering with unreachable handling** — Replaced `gather_facts: yes`
  with `gather_facts: no` + explicit `setup` task that has `ignore_unreachable: true`.  Hosts
  that fail fact gathering emit a `HOSTSTATUS: <hostname> | unreachable` debug line and hit
  `meta: end_host` so the play continues to the next host instead of aborting.
- **`HOSTSTATUS` parser in `ansible_runner.py`** — Recognizes the new `HOSTSTATUS:` debug lines
  from the playbook and marks hosts as unreachable in the parsed output.
- **`tags: always` on binary detection tasks** — `timeout_bin`, `brew_bin` stat/set_fact tasks
  now have `tags: always` so they run during both check and apply-updates plays.
- **Configurable data directory for k8s** — `install-config.yaml.example` gains
  `storage.dataDir` (default `/app-data`).  `install-k3s.sh` reads it into `PP_DATA_DIR` and
  substitutes it into all hostPath PV specs, cleanup Jobs, and uninstall commands.  Backend
  receives it via `PATCHPILOT_DATA_DIR` env var.
- **Expanded RBAC for k8s uninstall** — `00b-rbac.yaml` now grants: `deployments` get/list/patch,
  `deployments/scale` get/patch, `pods` get/list/watch, `jobs` get/list/watch/create/delete.
- **`/api/patch/status` timing info** — Response now includes `patch_running_seconds` and
  `check_running_seconds` for debugging stuck flags.
- **`/api/settings/system-info` returns `install_mode`** — Reports `docker` or `k3s` so the
  frontend can adapt labels and instructions.
- **Log ring buffer increased to 2000 entries** with noise filtering — High-frequency
  uvicorn access log lines (health checks, polling endpoints) are excluded from the buffer
  to preserve meaningful log history.
- **Docker Hub credential prompts in `build-push.sh`** — `_ensure_username()` interactively
  prompts for and optionally saves the Docker Hub username to `install-config.yaml`, matching
  the existing `_ensure_token()` pattern.
- **Web installer: credentials hidden in non-developer mode** — Docker Hub username/token fields
  are only shown when the server reports `developer: true`.  Non-developer installs pull from
  the public repo without credentials.
- **Web installer: auto-opens deployed URL** after successful install.

### Changed
- **Backup filename format** — Changed from `patchpilot_backup_YYYYMMDD_HHMMSS.tar.gz` to
  `patchpilot_YYYYMMDD_<4hex>.tgz`.  Shorter names, `.tgz` extension, random suffix to avoid
  collisions.  Legacy filenames are still recognized for listing, restore, download, and delete.
- **`.gitignore`** — Added `ansible/hosts` (generated inventory) and `k8s/install-config.yaml`
  (contains passwords/tokens/hostnames).
- **Frontend header** — Settings page header now shows the PatchPilot icon with a radial mask
  instead of the ⚙️ emoji.
- **Backup table layout** — Fixed column widths, responsive overflow, compact action buttons,
  human-readable file sizes.
- **Backup create endpoint** — Frontend now sends `description` and `include_encryption_key`
  as query parameters instead of a JSON body (fixes issues with some reverse proxy configs).
- **Deferred initial check** — Replaced fixed 60s sleep with a poll loop that waits until the
  DB has hosts (up to 60s), then runs the initial Ansible check.  Faster startup when hosts
  exist, no wasted check on empty installs.
- **Setup/restore file validation** — Accepts both `.tgz` and `.tar.gz` extensions throughout
  (setup.html, settings.html, setup_api.py, backup_restore.py).
- Version strings bumped to `0.9.7-alpha` across all files.

---

## [0.9.6-alpha] — 2026-02-27  (patch 3 — macOS / mas fixes)

### Fixed
- **`mas upgrade` hangs forever during patch run** — The "Apply App Store updates" Ansible task had no
  timeout. `mas upgrade` is completely silent during downloads (Xcode = ~15 GB), so the streaming
  readline loop produced zero output and the WebSocket UI appeared frozen until the overall 1800 s
  runner timeout fired and killed the whole run mid-download. The task now runs with
  `async: <mas_timeout_seconds>` + `poll: 30` so Ansible emits polling heartbeats every 30 s and
  the backend stream stays alive. The overall runner timeout is dynamically set to
  `max(1800, mas_timeout_seconds + 300)`.
- **Xcode (and other large apps) auto-updated by default** — `mas upgrade` with no arguments updates
  everything, including Xcode. Added `mas_excluded_ids` setting (default `497799835` — Xcode) so
  automated runs update apps individually and skip excluded IDs. Each app is upgraded via
  `mas upgrade <id>` rather than a bulk `mas upgrade`.
- **`mas` path hardcoded to `/opt/homebrew/bin/mas` (breaks Intel Macs)** — Replaced with a runtime
  `command -v` probe that tries `/opt/homebrew/bin/mas` (Apple Silicon), `/usr/local/bin/mas`
  (Intel), then bare `mas` in `$PATH`. All subsequent mas tasks reference `{{ mas_bin.stdout }}`.
- **Silent skip when mas is not installed** — Added an explicit `debug` warning task that tells the
  operator `mas not found on <host> — install: brew install mas` instead of silently passing
  with `failed_when: false`.
- **brew path not architecture-aware** — Homebrew is at `/opt/homebrew` on ARM and `/usr/local` on
  x86_64. All `brew` invocations now select the correct prefix via
  `{{ '/opt/homebrew/bin/brew' if ansible_architecture == 'arm64' else '/usr/local/bin/brew' }}`.

### Added
- **macOS / App Store settings section** (Settings → Network & Security → 🍎 macOS / App Store):
  - `mas_excluded_ids` — comma-separated App Store IDs to skip (default: Xcode `497799835`).
  - `mas_timeout_seconds` — per-host download timeout in seconds (default `7200` / 2 h).
  Both settings are stored in the `settings` table and loaded by `ansible_runner.py` into the
  subprocess environment before each patch run.

---

## [0.9.6-alpha] — 2026-02-27  (patch 2)

### Fixed
- **`users` table never created on fresh Docker install** — `run_auth_migration()` was loading
  `backend/migrations/002_add_authentication.sql` from disk, but the `migrations/` folder was
  never included in the Docker image (`COPY backend/ .` only copies Python files). The missing
  file was silently swallowed (`print("Migration file not found")`), leaving the `users`,
  `sessions`, and `audit_log` tables absent. Any attempt to complete first-run setup then bombed
  with `asyncpg.exceptions.UndefinedTableError: relation "users" does not exist`. SQL is now
  inlined directly in `run_auth_migration()` — no file dependency (`backend/app.py`).
- **Restore applies wrong encryption key after backend restart** — The `/api/setup/restore`
  endpoint correctly wrote the backup's encryption key to `/install/.env`, but then triggered a
  restart via `docker restart <container-id>`. Docker's `restart` does **not** re-read `.env` —
  environment variables are baked into the container at creation time. The backend therefore came
  back up with the old key, causing every encrypted credential (SSH keys, passwords) to fail
  decryption. Restart now uses `docker compose -f <compose_file> up -d backend` so Compose
  re-evaluates `.env` and the restored key is active immediately (`backend/setup_api.py`).
- **Version bump to v0.9.6-alpha** across all components
  (`backend/app.py`, `frontend/index.html`, `webinstall/server.py`,
  `webinstall/static/index.html`).

---

## [0.9.6-alpha] - 2026-02-26

### Fixed
- **Delete-policy PVs stuck in `Failed` after uninstall** — Static hostPath PVs use `kubernetes.io/no-provisioner`; there is no provisioner to answer the Delete reclaim callback, so Kubernetes immediately marks them `Failed`. The uninstall now explicitly runs `kubectl delete pv` on all `Delete`-policy PVs after namespace teardown — exactly what `kubectl delete pv <name>` does manually — instead of waiting 90 s for a provisioner callback that will never arrive (`uninstall_api.py`, `install-k3s.sh`).
- **Reinstall falsely pausing for node cleanup when only `patchpilot-backups` dir exists** — The stale-data SSH check used `find patchpilot-*` which matched the intentionally retained `patchpilot-backups` directory, causing the web installer to pause and demand manual cleanup on every reinstall. Check and cleanup commands now use `! -name 'patchpilot-backups'` to exclude it (`install-k3s.sh`).
- **Uninstall cleanup Job deleting backup archives** — The busybox hostPath cleanup Job ran `find patchpilot-* -exec rm -rf` which also removed `/app-data/patchpilot-backups`. Job command updated with `! -name 'patchpilot-backups'` exclusion so backup files survive uninstall as intended (`install-k3s.sh`).
- **Cleanup `rm` commands nuking backup dir** — Both `cleanup_cmd` assignments (initial and SSH-target-resolved) used `sudo rm -rf /app-data/patchpilot-*`. Changed to explicitly name `patchpilot-postgres-data` and `patchpilot-ansible-data` (`install-k3s.sh`).
- **`patchpilot-backups` PV stuck `Released` blocking reinstall** — A `Released` PV retains its old `claimRef` (including PVC UID); a new PVC with `volumeName:` set will not bind until `claimRef` is cleared. `apply_manifests()` now detects a `Released` backups PV before applying manifests and patches out `claimRef`, transitioning it to `Available` so the new install binds cleanly with existing backup data intact (`install-k3s.sh`).
- **crictl image cleanup only warned, never ran** — Uninstall printed the crictl command as a manual step but never executed it. Uninstall now SSHes to the node and runs `k3s crictl rmi` automatically; falls back to a `__NOTE_CLEANUP__` message only if SSH is unreachable (`install-k3s.sh`).

### Changed
- Uninstall background task ordering in `_k8s_cleanup_background` tightened: workloads scaled to zero → PVCs deleted → namespace deleted → Delete-policy PV objects explicitly removed → ClusterIssuer removed.

---

## [0.9.5-alpha] - 2026-02-26

### Added
- **Web-based Uninstaller (Settings → Advanced → Danger Zone)** — Admin users can now initiate a full PatchPilot uninstall directly from the UI. The system auto-detects the install type (Docker Compose or Kubernetes/k3s) and presents a two-phase workflow:
  - **Preview phase** — shows exactly which steps will be automated vs which require manual host access, before anything is executed.
  - **Confirmation gate** — operator must type `UNINSTALL` to proceed, preventing accidental execution.
  - **Execution phase** — animated progress bar while the backend runs; shows completed/failed steps on completion.
  - **Manual commands panel** — any steps the backend cannot perform (hostPath cleanup, removing the repo directory, optional full k3s/Docker removal) are displayed in a copyable code block.
- **`backend/uninstall_api.py`** — New FastAPI router (`/api/uninstall/status`, `/api/uninstall/execute`). Admin-only. Detects install type via env var `PATCHPILOT_INSTALL_MODE`, k3s kubeconfig presence, or Docker socket. Docker uninstall runs `docker compose down -v`, prunes patchpilot images/volumes. K3s uninstall delegates to existing `k8s/install-k3s.sh --uninstall` (with `NO_INTERACTIVE=true`) or direct `kubectl delete namespace` fallback.
- **Docker uninstall script support** — Docker Compose installs previously had no equivalent to `k8s/install-k3s.sh --uninstall`. The new API handles Docker teardown natively.
- **`PACKAGE:` output in check playbook** — `ansible/check-os-updates.yml` now emits `PACKAGE: hostname | <pkg_line>` debug entries for all package managers (apt, brew, softwareupdate, mas) after update discovery. This populates the `packages` table so the **Update Types** dashboard chart and host **Details** package list are no longer always empty.
- **Phased update support** — Hosts running Ubuntu with phased package rollouts showed pending updates on the dashboard but patching silently did nothing. The check playbook now includes `APT_GET_ALWAYS_INCLUDE_PHASED_UPDATES=1` and the patch task uses `apt-get -o APT::Get::Always-Include-Phased-Updates=true` to force phased packages through.

### Fixed
- **`become_password` with special characters** — `ansible_runner.py` passed the sudo password as a raw `--extra-vars key=value` string which Ansible parses as YAML. Passwords containing `!`, `#`, `{`, `:`, `@` and other YAML-significant characters were silently corrupted, causing `become` authentication to fail and apt upgrades to be skipped. Fixed by passing `--extra-vars` as a JSON string.
- **False-positive "patched" detection** — `_detect_hosts_actually_patched()` accepted Ansible `ok:` as confirmation of a successful patch. `ok:` from the apt module means the task ran but installed zero packages. Only `changed:` indicates packages were actually written to disk. Fixed to require `changed:` exclusively; the overly permissive PLAY RECAP fallback (which marked any host with `ok > 0` as patched) was removed.
- **Stale apt cache causing check/patch disagreement** — The check playbook ran `apt list --upgradable` against the on-disk cache (potentially days old). The patch playbook ran `apt-get update` first, got a fresh view, and found nothing to do — so patching appeared to succeed while the dashboard still showed pending packages. Fixed by adding a cache refresh step at the start of the check playbook.
- **Cache refresh task breaking host status** — The new `apt-get update` step in the check playbook uses `become: yes`. Check runs do not supply a sudo password, causing Ansible to mark `failed=1` in the PLAY RECAP and the backend to set host status to `failed`. Fixed with `failed_when: false` and `ignore_errors: true`.
- **Update Types chart always empty** — Direct consequence of missing `PACKAGE:` output lines. The `packages` table was never populated so the donut chart always showed "No data available."

### Changed
- `ansible/check-os-updates.yml` — apt cache refresh added (non-fatal); `PACKAGE:` emit tasks added for all OS types; apt upgrade task replaced with shell invocation for phased update support and correct `changed_when` detection.
- `backend/ansible_runner.py` — `--extra-vars` now uses JSON encoding for become password.
- `backend/app.py` — `_detect_hosts_actually_patched()` now requires `changed:` only; PLAY RECAP fallback removed; `ok:` results emit a `[WARN]` log entry. Registers the new `uninstall_router`.
- `frontend/settings.html` — Advanced tab now includes a **Danger Zone** section with the Uninstall PatchPilot button and a three-step modal (confirm → progress → results).

---

## [0.9.4-alpha] - 2026-02-24

### Added
- **File upload for SSH keys in setup wizard** — Step 6 of `setup.html` now has a "📂 Upload Key File" button alongside the paste textarea. Uses `FileReader` to load the key directly from disk — no clipboard, no truncation. Auto-fills the key name from the filename.
- **Hosts created during setup get default key** — `setup_api.py` now sets `ssh_key_type='default'` on all hosts created during the setup wizard so they automatically resolve the saved default key without manual assignment.
- **`seed-ansible` init container** — `k8s/templates/04-backend.yaml` now includes a `seed-ansible` init container that copies playbooks from the image (`/ansible-src/`) to the PVC on first deploy using `cp -rn` (no-clobber, safe on redeploy).

### Fixed
- **Settings → Hosts 500 error on fresh install** — `settings_api.py` queried columns named `ssh_private_key_encrypted` / `ssh_password_encrypted` but the DB schema created them as `ssh_private_key` / `ssh_password`. Fixed `ensure_core_tables` to create correctly named BYTEA columns and added `ensure_hosts_columns` migration that renames old columns on existing installs.
- **Ansible playbooks missing from PVC after install** — `Dockerfile` now copies `ansible/` into the image at `/ansible-src/` so the `seed-ansible` init container has playbooks to seed from.
- **SSH key `error in libcrypto` on all key paths** — OpenSSH requires private key files to end with `
`. All three temp-file write sites (test connection + two Ansible inventory paths) now normalize CRLF line endings and append `
` if missing, fixing failures caused by browsers stripping the trailing newline from pasted keys.
- **Default SSH key not resolved for Ansible checks** — Hosts with `ssh_key_type='default'` were connecting with no key, always showing `unreachable`. The Ansible inventory builder now pre-fetches the default key from `saved_ssh_keys WHERE is_default=TRUE` and injects it for any host using the default.
- **Default SSH key not resolved for test connection** — `test_connection` in `settings_api.py` now resolves `key_type='default'` to the saved default key before attempting SSH.
- **Frontend API calls broken on `localhost`** — `login.html`, `settings.html`, and `app.js` hardcoded `http://localhost:8000/api` when `window.location.hostname === 'localhost'`, bypassing nginx and causing "Connection error. Is the backend running?" on Docker Compose installs. All API references now use relative `/api` paths.
- **WebSocket URL** — `app.js` now derives the WebSocket base URL from `window.location` and correctly upgrades to `wss://` when served over HTTPS.
- **PVC `storageClassName` immutability error on reapply** — `kubectl apply` failed on existing installs because manifests omitted `storageClassName`. All three PVCs in `k8s/templates/02-pvcs.yaml` now include `storageClassName: app-data`.

### Changed
- `database-schema.sql` updated: `ssh_private_key_encrypted BYTEA` / `ssh_password_encrypted BYTEA` to match application code.
- SSH key file upload added to Settings → SSH Keys UI.

---

## [0.9.3-alpha] - 2026-02-23

### Fixed
- **imagePullPolicy changed to `Always`** — prevents k3s containerd from serving stale cached images when namespace/PVs are wiped and app is redeployed with the same tag.

### Added
- `k8s/nuke-data.sh` — wipes all PatchPilot hostPath data dirs and purges containerd image cache on the k3s node before a clean reinstall.

### Changed
- Version bumped to `0.9.3-alpha` in `install-config.yaml`, `install-k3s.sh`, and all generated manifests.

---

## [0.9.2-alpha] - 2026-02-21

### Added
- **K3s / Kubernetes install path** — full native k3s deployment alongside existing Docker Compose option
  - `install.sh` prompts for install method; `--docker` and `--k3s` flags for non-interactive use
- **`k8s/install-config.yaml`** — single YAML config for all deployment settings
- **`k8s/install-k3s.sh`** — fully automated k3s installer with `--interactive`, `--dry-run`, and `--uninstall` modes
- **`Dockerfile.frontend`** — separate frontend image (nginx + baked-in static assets)
- **Kubernetes manifest templates** (`k8s/templates/00–09`) — namespace, secrets, PVCs, Postgres, backend, frontend, Traefik middlewares, cert-manager Certificate, Ingress, ClusterIssuers
- **HSTS and security headers** — applied at Traefik middleware layer

### Changed
- `install.sh` refactored with install-method selection menu
- Docker Compose path now auto-generates Fernet key
- Nginx config updated: backend proxied to `patchpilot-backend` service name

### Fixed
- PostgreSQL readiness probe includes `-d <dbname>` to avoid false negatives on first start
- Frontend image in k3s uses correct `imagePullPolicy` for local strategy

---

## [2.0.0] - 2026-02-11

### Added
- Saved SSH Keys Library — store, reuse, upload, and set defaults; AES-256 encrypted at rest
- Real-time WebSocket patching progress — live Ansible task output with per-task timestamps
- Single-host check API — `/api/check/{hostname}`, auto-triggered on host creation
- Auto-reboot management — per-host configurable; control node always protected
- macOS system update and App Store detection

### Changed
- Background check interval reduced to 2 minutes
- SSH ControlMaster disabled

### Fixed
- macOS package type label, SSH key BYTEA/TEXT mismatch, package version parsing

### Security
- Temporary SSH key files created with `0600` permissions and deleted after use

---

## [1.0.0] - 2026-01-15

### Added
- Initial release
- Multi-platform support: Debian/Ubuntu, RHEL/CentOS, macOS
- Host management, encrypted SSH credential storage, dashboard, settings

---

**Legend:** Added · Changed · Deprecated · Removed · Fixed · Security
