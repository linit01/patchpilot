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
