# Changelog

All notable changes to PatchPilot will be documented in this file.

---

## [0.13.13-alpha] — 2026-03-24

### Fixed
- **Windows Update (PSWindowsUpdate) packages no longer show UNKNOWN badge**: added `winupdate` parser in `ansible_runner.py` that detects `KB#` in PACKAGE lines and extracts a clean title; PSWindowsUpdate check in `check-os-updates.yml` now emits structured `Title (KB#) installed -> available` format
- **macOS system update duplicates eliminated**: `softwareupdate -l` Title lines (tab-prefixed duplicates of `* Label:` lines) are now detected and skipped — handles both real tab characters and Ansible's literal `\t` escaping
- **Show update status combines Windows sources**: the Jinja2 `elif` chain that hid winupdate counts when winget packages existed is replaced with a combined block that sums both sources into one total
- **Ghost host bug**: deleted hosts no longer reappear after the next periodic check — `app.py` now snapshots current DB hostnames before the upsert loop and skips any hostname that was removed while Ansible was in-flight

---

## [0.13.12-alpha] — 2026-03-24

### Fixed
- **Windows Update packages parsed as `winupdate` type**: first pass at KB-based parser for PSWindowsUpdate PACKAGE lines (refined in 0.13.13)
- **macOS `Title:` line noise filter**: initial skip for `softwareupdate -l` duplicate Title lines (tab escaping fix in 0.13.13)

---

## [0.13.11-alpha] — 2026-03-24

### Added
- **Windows Update support via PSWindowsUpdate**: new scheduled task `PP-WinUpdate` for checking and applying OS-level Windows Updates alongside winget app updates; `winupdate_enabled` setting (default: false) gates the feature; `Enable-PatchPilotSSH.ps1` bootstraps the PSWindowsUpdate module and creates the scheduled task
- **Separate winget and winupdate patching tasks**: `check-os-updates.yml` now has distinct check/apply paths for winget (app-level) and Windows Update (OS-level), each with their own output files and scheduled tasks

---

## [0.13.10-alpha] — 2026-03-23

### Fixed
- **Dashboard donut chart colors**: update type chart now uses a stable color palette instead of random assignment; `unknown` gets a distinct grey
- **Homebrew patch activity tracking**: `_extract_patched_packages()` Strategy 4 now parses brew upgrade output (`==> Upgrading pkg` + version lines) so Homebrew patches appear in patch history with correct counts
- **Copy button reliability**: clipboard copy for SSH keys and setup commands no longer fails silently on non-HTTPS contexts

---

## [0.13.9-alpha] — 2026-03-23

### Added
- **Windows agent setup endpoint**: `/api/setup/windows-agent` serves the `Enable-PatchPilotSSH.ps1` script with the default SSH public key pre-embedded — users download one script and run it, no manual key copy needed
- **"Add Windows Host" button**: Hosts page now shows a dedicated button that opens a modal with the PowerShell setup command and copy button

### Fixed
- **Connection test OS auto-detect**: `os_family` from the SSH connection test is now correctly passed through both the create and edit host flows

---

## [0.13.8-alpha] — 2026-03-23

### Added
- **Windows agent setup endpoint and Add Windows Host button**: initial implementation (refined in 0.13.9)
- **`Enable-PatchPilotSSH.ps1` improvements**: winget source agreement acceptance during setup, network profile auto-fix for Public → Private, PSWindowsUpdate pre-install step

---

## [0.13.7-alpha] — 2026-03-23

### Fixed
- **`os_family` auto-detect now works on both create and edit**: previously `os_family` from the connection test was only passed during host creation; if a host was re-added (edit path), it was silently dropped, causing the first host check to fail with `unreachable` because Ansible didn't know to use PowerShell
- **`backend/settings_api.py`**: `HostUpdate` model now accepts `os_family`; the dynamic UPDATE query includes `os_family` when provided
- **`frontend/settings.html`**: `os_family` from the connection test is now included in both create and edit save requests

---

## [0.13.6-alpha] — 2026-03-22

### Added
- **Live winget package streaming in patch logs**: patch output now shows per-package status during Windows patching — `📦 [host] [1/4] PackageId (ver)`, `✅ installed`, `❌ failed (exit code)`, `⚠️ skipped (different install technology)`
- **Windows reboot handling**: after winget patching, checks registry keys and winget output for reboot indicators; reboots the host if `Allow Auto-Reboot` is enabled, matching Linux behavior
- **`allow_auto_reboot` respected on both Linux and Windows**: reboot tasks now check the per-host setting; if disabled, host shows `Reboot Required: Yes` without rebooting

### Changed
- **SSH connection timeout reduced to 10 seconds** for both check and patch runs — improves scan time when offline hosts are in the inventory
- **Async progress messages** now use generic "Package updates in progress" instead of hardcoded "App Store downloads"

### Fixed
- **Winget patch log package count**: `_extract_packages_updated()` now parses `stdout_lines` from the Ansible JSON blob (Strategy 3) — previously the single-line JSON format caused the sequential `Found`/`Successfully installed` tracker to fail, resulting in 0 packages recorded

---

## [0.13.3-alpha] — 2026-03-22

### Added — Windows Phase 2: Exclusions & Auto-Detect
- **Windows/Winget exclusion settings**: new Settings > General > Windows/Winget section with pill-based UI for managing excluded winget package IDs, mirroring the existing macOS/App Store exclusion pattern; `Microsoft.Edge` is pre-filled as the default exclusion because Edge uses a different install technology and cannot be upgraded via winget
- **Auto-detect Windows on host creation**: the connection test stores `detected_os` in the frontend; when saving a new host, `os_family` is included in the create request so the first host check works immediately without manual SQL bootstrapping
- **Network profile auto-fix in PS setup script**: `Enable-PatchPilotSSH.ps1` now detects Public network profiles and switches them to Private automatically, preventing the SSH connectivity issue that blocked connections on fresh Windows installs

### Changed
- **`backend/app.py`**: seeds `winget_excluded_ids` setting with `Microsoft.Edge` default on startup
- **`backend/settings_api.py`**: `HostCreate` model accepts optional `os_family`; INSERT includes `os_family` from connection test auto-detection
- **`backend/ansible_runner.py`**: passes `WINGET_EXCLUDED_IDS` env var to both check and patch playbook runs from DB settings
- **`ansible/check-os-updates.yml`**: winget check task filters excluded package IDs before emitting PACKAGE lines; added `winget_excluded_ids` playbook var from env; apply task uses `--force` flag
- **`frontend/settings.html`**: Windows/Winget exclusion pill UI with add/remove/save; connection test captures `detected_os` and passes it to host creation; `loadGeneralSettings` loads `winget_excluded_ids` on page init
- **`scripts/windows/Enable-PatchPilotSSH.ps1`**: auto-detects and fixes Public network profiles to Private before firewall configuration

---

## [0.13.1-alpha] — 2026-03-21

### Added — Windows Host Support (Phase 1)
- **Windows client management via SSH**: PatchPilot can now manage Windows 10/11 hosts over OpenSSH with PowerShell as the remote shell
- **`Enable-PatchPilotSSH.ps1` setup script**: creates a dedicated `patchpilot` local admin service account (hidden from login screen, random password, key-only auth), installs/configures OpenSSH Server, sets firewall rules (all profiles), configures `sshd_config` for public key auth, sets PowerShell as default SSH shell, installs the SSH public key in both user and `administrators_authorized_keys`, optionally installs PSWindowsUpdate module, creates `PP-WingetCheck` scheduled task for winget update detection, and initializes winget source agreements — all in a single 10-step automated script
- **Winget app update detection**: Ansible playbook checks for available winget updates on Windows hosts via a scheduled task mechanism (required because winget's MSIX sandbox does not work in non-interactive SSH sessions); detected updates are displayed in the dashboard with `WINGET` type badges, package IDs, and version numbers
- **Winget app patching**: `PATCH THIS HOST` applies all available winget updates via `winget upgrade --all` using the scheduled task approach; temporarily swaps the `PP-WingetCheck` task action to an apply command, waits up to 30 minutes for completion with Ansible async/poll heartbeats, captures full output, then restores the original check action
- **`ansible.windows` collection**: added to Dockerfile so Windows fact gathering and `win_shell` module work out of the box
- **Auto-detect Windows OS on connection test**: the SSH connection test runs `echo $env:OS` to detect Windows hosts and automatically sets `os_family=Windows` in the database, eliminating manual bootstrapping
- **IP address fallback**: when `ansible_default_ipv4` is unavailable (common on Windows), the parser uses the hostname as the IP address if it's already an IPv4 address

### Changed
- **`backend/ansible_runner.py`**: Windows hosts receive `ansible_shell_type=powershell`, `ansible_connection=ssh`, and `ansible_become_method=runas` in the dynamic inventory; HOSTINFO regex updated to capture multi-word OS names (e.g., "Microsoft Windows 11 Home"); winget package parser added with permissive regex that handles winget's Unicode-truncated names; PACKAGE line parser now strips trailing JSON quotes
- **`backend/settings_api.py`**: connection test command changed from `uname -a` (Linux-only) to `hostname` (cross-platform); OS detection via `echo $env:OS` with auto-write to database
- **`backend/database.py`**: `upsert_host` uses `CASE WHEN` to preserve existing `os_family`, `os_type`, and `ip_address` when new values are empty — prevents failed checks from blanking known OS info
- **`ansible/check-os-updates.yml`**: added Windows winget check tasks (via scheduled task trigger, output file parsing), winget package emit, winget status reporting, and winget apply-updates tasks
- **`Dockerfile`**: added `RUN ansible-galaxy collection install ansible.windows`

### Fixed
- **`backend/update_checker.py`**: `_read_version()` now matches `app.py` priority order (VERSION file first, `APP_VERSION` env fallback) — previously the reversed order caused the update checker to report stale versions after in-app upgrades
- **`backend/update_checker.py`**: k8s `_apply_update_k8s()` now patches the `APP_VERSION` environment variable on the backend deployment via `kubectl patch` with strategic merge — previously only the image tag was updated, leaving the env var stale

---

## [0.12.0-alpha] — 2026-03-17

### Added — LemonSqueezy License Validation
- **License activation via LemonSqueezy**: `POST /api/license/activate` calls LemonSqueezy's License API to activate the key with a unique install UUID, binding the key to one installation
- **Machine binding**: install UUID generated on first setup, persisted in database; LemonSqueezy enforces `activation_limit: 1` — key sharing across machines is blocked
- **License validation**: `POST /api/license/validate` endpoint for manual re-validation; confirms key status (active, expired, disabled) with LemonSqueezy
- **License deactivation**: `POST /api/license/deactivate` calls LemonSqueezy to free the activation slot, allowing the key to be used on a different machine
- **Periodic background validation**: every 7 days, the backend validates the license with LemonSqueezy to detect subscription expiry or admin revocation
- **30-day grace period**: if LemonSqueezy API is unreachable, the cached validation allows continued use for up to 30 days before blocking
- **Customer info display**: customer name and email from LemonSqueezy stored and shown in the License settings tab
- **Subscription management**: subscription expiry, cancellation, and admin-disabled states detected via validation and reflected in the app status

### Changed
- **`backend/license.py`** rewritten: replaced placeholder "any key works" logic with full LemonSqueezy License API integration using `httpx`
- **`backend/app.py`**: added `periodic_license_check()` background task launched at startup

---

## [0.11.1-alpha] — 2026-03-17

### Added — License & Trial System
- **14-day trial**: starts automatically when first-run setup completes; stored in settings table
- **Trial banner**: dashboard shows amber "Trial: X days remaining" banner with link to purchase
- **Trial expired overlay**: full-screen blocking overlay when trial ends, with purchase link and license key entry
- **License key activation**: Settings → License tab with status display, key input, activate/deactivate
- **`backend/license.py`** — new module: `start_trial()`, `get_license_status()`, `enforce_license()`, `enforce_trial_active()`
- **API endpoints**: `GET /api/license/status`, `POST /api/license/activate`, `POST /api/license/deactivate`
- **Backup/restore gated**: create, download, upload, restore, and delete backup endpoints return 403 without an active license (trial users see a lock overlay on the Backup & Restore tab)
- **`LICENSE`** file — proprietary software license replacing MIT

### Added — Landing Page & Installer
- **`getpatchpilot.app`** — landing page on Cloudflare Pages with LCARS-themed design, feature cards, deployment paths, and screenshot gallery with click-to-expand lightbox
- **`curl | bash` bootstrap installer** — `curl -fsSL https://getpatchpilot.app/install.sh | bash` downloads PatchPilot via git clone or release tarball, detects piped vs interactive mode, auto-selects download method when non-interactive
- **Web wizard is now the default** install mode (option 1 when running `./install.sh` with no flags)

### Changed
- **License badge**: README badge changed from MIT to Proprietary
- **Install mode order**: `./install.sh` interactive menu now lists Web Wizard first (default), then Docker Compose, then K3s

### Removed
- **Orphan files**: `NOTES`, `install_dependencies.sh`, `install.html.installer`, `push_new_build.sh.old`, `CHANGELOG-v0.9.7a.md`, `k8s/install-k3s.sh.orig`, `k8s/install-k3s.sh.rej`

### Security
- **Repo sanitized**: personal data (emails, IPs, hostnames, paths) scrubbed from all files and git history via `git filter-repo`
- **`.gitignore` hardened**: added entries for `.env`, generated certs, tarballs, and developer-only scripts
- **GitHub security features enabled**: Dependabot alerts, secret protection, push protection

---

## [0.11.0-alpha] — 2026-03-16

### Added — Multi-User Role-Based Access Control (RBAC)
- **Three-tier role model**: `full_admin` (app owner, exactly one), `admin` (manage own resources), `viewer` (read-only)
- **Resource ownership**: `created_by` column added to `hosts`, `saved_ssh_keys`, and `patch_schedules` tables with automatic backfill on upgrade
- **API-level scoping**: all host, SSH key, schedule, stats, charts, alerts, and patch-history endpoints filtered by ownership for `admin` users
- **Write guards**: `viewer` role blocked from all mutating endpoints (create, update, delete, patch, check)
- **Full Admin filter dropdown**: `[All Users ▾]` dropdown on Dashboard for `full_admin` to view resources by owner
- **Owner column**: Dashboard hosts table, Settings → Hosts, SSH Keys, and Schedules all show resource owner (full_admin only)
- **Sidebar scoping**: `admin` sees only Manage Hosts, SSH Keys, Schedules; `viewer` sees no management links or action buttons
- **Settings tab scoping**: `admin` hidden from General, Users, Advanced, Backup, Updates tabs; `viewer` redirected to dashboard
- **Role badges**: User management table shows "Full Admin" / "Admin" / "Viewer" with color-coded badges; delete button hidden for full_admin account
- **`backend/rbac.py`** — new module centralizing ownership helpers (`owner_id`, `verify_host_ownership`, `verify_schedule_ownership`, `verify_ssh_key_ownership`)
- **Startup migration**: `ensure_rbac_columns()` adds `created_by` columns, migrates `admin` → `full_admin` role, backfills existing resources to the app owner

### Added — Debug Logging Toggle
- **Pill switch** in Settings → Advanced to enable/disable verbose debug logging at runtime
- **`GET/PUT /api/debug`** endpoints (full_admin only) to read/toggle debug mode
- **Persisted to DB** via `debug_mode` setting — survives container restarts
- **Runtime effect**: toggles Python log levels on root + third-party loggers (uvicorn, asyncpg, httpx, paramiko) between DEBUG and INFO without restart

### Added — Docker Hub Update Fallback
- **Update checker fallback**: when GitHub Releases API returns 404/403 (private repo, no token), automatically falls back to Docker Hub Tags API
- Queries `hub.docker.com/v2/repositories/linit01/patchpilot/tags` for the latest `backend-*` tag
- Eliminates the need for `GITHUB_TOKEN` on Kubernetes deployments

### Changed
- **Email field made optional**: `users.email` column is now nullable; setup wizard no longer requires email; auto-generates `{username}@patchpilot.local` when not provided; Docker and k3s installers are now consistent
- **Ansible playbook path fields removed** from Docker web installer, k3s web installer, `webinstall/server.py`, `install-k3s.sh`, and config YAML files — playbook is baked into the Docker image and synced at startup
- **Backup retention logic rewritten**:
  - Uninstall backups (`*_uninstall.tgz`) excluded from retention count and never pruned
  - Companion `_ENCRYPTION_KEY.txt` files deleted when their archive is pruned
  - At least one encryption-key-bearing backup preserved (only if no kept backup has the key)
  - Previously, all backups with encryption keys were skipped, causing retention to never clean up
- **Backup upload endpoint** now accepts both `.tar.gz` and `.tgz` files (was `.tar.gz` only)
- **Backup health endpoint** disk size calculation now counts both `.tgz` and `.tar.gz` files
- **`require_admin`** now accepts both `full_admin` and `admin` roles
- **Uninstall, Backup/Restore, Settings, Update endpoints** restricted to `require_full_admin`
- **User management** restricted to `full_admin` only; cannot create another `full_admin` or delete the `full_admin` account

### Fixed
- **Sensitive data in logs**: converted 15+ `print()` statements in `ansible_runner.py` to proper `logger.debug()` calls; **removed SSH private key content** that was being printed to stdout (first 50 chars of decrypted key)
- **Test connection debug prints**: converted all diagnostic `print()` statements in `settings_api.py` to `logger.debug()`, controlled by the debug toggle
- **Encryption test harness**: removed `print(f"Decrypted: {decrypted}")` from `encryption_utils.py` test block
- **Owner column race condition**: Settings page data loads (hosts, SSH keys, system info) moved to after auth check completes, so `_ppUserRole` is set before tables render
- **Sidebar version update badge**: fixed not showing without manual refresh

### Security
- **SSH key content no longer logged**: `ansible_runner.py` line 129 previously printed `Decrypted key for {hostname}: {decrypted_key[:50]}...` to stdout on every Ansible check — removed
- **All debug output now gated**: sensitive diagnostic prints converted to `logger.debug()` and controlled by the debug toggle (off by default)
- **Viewer role enforced at API level**: all write endpoints return 403 for viewer role, not just hidden in UI

---


## [0.10.0-alpha] — 2026-03-12

### Added
- **In-app update checker and upgrade system** — PatchPilot can now detect new releases
  via the GitHub Releases API and apply updates directly from the Settings → Updates tab.
  - **Sidebar badge** — a pulsing cyan "Update available" indicator appears beneath the
    version tag when a newer release is found. Re-checks on every dashboard refresh cycle.
  - **Settings → Updates tab** — shows current vs latest version, release notes, channel
    (pinned or latest), install mode (Kubernetes or Docker Compose), and an "Update Now" button.
  - **Configurable check interval** — enable/disable automatic checks; interval options from
    1 hour to 1 week. Stored in the `settings` table.
  - **Kubernetes update path** — uses `kubectl set image` on both backend and frontend
    deployments, then `kubectl rollout restart` to pick up new images.
  - **Docker Compose update path** — rewrites image tags in `docker-compose.yml`, pulls new
    images with `docker pull`, then spawns a `docker:cli` helper container (same pattern as
    uninstall) that stops the old containers and runs `docker compose up -d` to bring up new
    ones. Avoids the "container can't restart itself" problem.
  - **Frontend progress UI** — progress bar with reconnect handling; polls the backend after
    restart to detect the version change and auto-reloads the page.
  - **Private repo support** — reads `GITHUB_TOKEN` from environment (env-only, never exposed
    in UI or docs) for authenticated GitHub API access.
- **CI/CD: automatic GitHub Release creation** — `softprops/action-gh-release@v2` with
  `generate_release_notes: true` added to the Docker build-push workflow. Releases are now
  created automatically when a `v*` tag is pushed. Workflow permissions upgraded from
  `contents: read` to `contents: write`.
- **Backup: standalone encryption key file** — when "Include encryption key in backup" is
  checked, a `<backup_name>_ENCRYPTION_KEY.txt` file is now written alongside the `.tar.gz`
  in the backup directory. Operators can grab the key without extracting the tarball.
- **Backup: retention protection for key-bearing backups** — `_enforce_retention()` now
  checks `backup_metadata.json` inside each archive before deletion. Backups that include
  the encryption key are never pruned by the retention policy.
- **`docker-compose-plugin`** added to the backend Dockerfile so `docker compose` (v2) works
  inside the container for the update helper.
- **`backend/update_checker.py`** — new module: GitHub API polling, version comparison
  (PEP 440 via `packaging`), update execution for both k8s and Docker, all API endpoints
  (`/api/updates/status`, `/check`, `/apply`, `/progress`).
- **`scripts/push_new_build.sh`** — helper script to automate version bump, tag, and push
  with confirmation prompts and duplicate tag handling.
- **`scripts/claude-context.sh`** — generates a base64-encoded tarball of the codebase for
  Claude AI chat sessions (excludes secrets, venvs, node_modules).

### Fixed
- **Version display on sidebar** — `app.js` now strips `-alpha`/`-beta` suffix before
  displaying in the sidebar version tag (the HTML has a separate badge for the pre-release
  label). Fixed fallback from em-dash to proper `v—` when API is unreachable.
- **Install mode detection** — `PATCHPILOT_INSTALL_MODE=k8s` is now normalized to `k3s`
  throughout the codebase (`update_checker.py`, consistent with `uninstall_api.py`'s
  cascading detection: env var → k3s kubeconfig → service account token → Docker markers).
- **`kubectl set env` pollution** — documented that `kubectl set env` bakes values into the
  deployment spec, overriding image-level `ENV` on future image tag updates. Update code
  avoids this pattern.

### Changed
- **GitHub Actions workflow** (`docker-build-push.yml`) — `permissions.contents` changed
  from `read` to `write`; added `softprops/action-gh-release@v2` step for automatic
  release creation with auto-generated changelogs.

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
- **macOS check fails with `'timeout_bin' is undefined` when mas enabled** — The "Warn if no
  timeout binary" task in `check-os-updates.yml` referenced `timeout_bin.stdout` instead of
  `timeout_bin_path`.  With `mas_enabled=false` the bug was hidden by short-circuit evaluation.

### Added
- **macOS system update detection and configurable install** — New `macos_system_updates_enabled`
  setting (default `false`).  When disabled, PatchPilot detects available macOS system updates
  and reports them as `macos-system` package type but does not attempt to install them.
  When enabled, non-control nodes get `softwareupdate -iaR` and control nodes get download-only.
- **macOS system update alerts** — Dashboard alerts now include an `info`-severity alert type
  for hosts with detected macOS system updates, with blue styling in the frontend.
- **Backup configuration panel** (Settings → Backup & Restore) — New UI card to view/change
  backup storage type (local / NFS), NFS server/share, and retention count.
- **`apt-get update` before patching** — Debian/Ubuntu patch runs now refresh the apt cache
  before applying updates.
- **Ansible `--forks 5` for check, `--forks 1` for patch** — Check playbook runs with 5 forks
  for faster parallel host checking.  Patch playbook uses 1 fork (serial).
- **Ansible explicit fact gathering with unreachable handling** — Replaced `gather_facts: yes`
  with explicit `setup` task with `ignore_unreachable: true`.
- **`HOSTSTATUS` parser in `ansible_runner.py`** — Recognizes `HOSTSTATUS:` debug lines.
- **`tags: always` on binary detection tasks** — `timeout_bin`, `brew_bin` tasks.
- **Configurable data directory for k8s** — `storage.dataDir` in config.
- **Expanded RBAC for k8s uninstall** — deployments, pods, jobs verbs.
- **`/api/settings/system-info` returns `install_mode`**.
- **Log ring buffer increased to 2000 entries** with noise filtering.
- **Web installer: credentials hidden in non-developer mode**.
- **Web installer: auto-opens deployed URL** after successful install.

### Changed
- **Backup filename format** — Changed from `patchpilot_backup_YYYYMMDD_HHMMSS.tar.gz` to
  `patchpilot_YYYYMMDD_<4hex>.tgz`.
- **Deferred initial check** — Replaced fixed 60s sleep with a poll loop.
- **Setup/restore file validation** — Accepts both `.tgz` and `.tar.gz` extensions.

---

## [0.9.6-alpha] — 2026-02-27 (patch 3 — macOS / mas fixes)

### Fixed
- **`mas upgrade` hangs forever during patch run** — Added `async` + `poll: 30` timeout.
- **Xcode auto-updated by default** — Added `mas_excluded_ids` setting (default Xcode).
- **`mas` path hardcoded to ARM path** — Runtime `command -v` probe for both architectures.
- **Silent skip when mas not installed** — Added explicit debug warning task.
- **brew path not architecture-aware** — Now selects correct prefix via `ansible_architecture`.

### Added
- **macOS / App Store settings section** — `mas_excluded_ids`, `mas_timeout_seconds` settings.

---

## [0.9.6-alpha] — 2026-02-27 (patch 2)

### Fixed
- **`users` table never created on fresh Docker install** — SQL now inlined in
  `run_auth_migration()`.
- **Restore applies wrong encryption key after backend restart** — Now uses
  `docker compose up -d backend` instead of `docker restart`.

---

## [0.9.6-alpha] — 2026-02-26

### Fixed
- **Delete-policy PVs stuck in `Failed` after uninstall** — Explicit `kubectl delete pv`.
- **Reinstall falsely pausing for node cleanup** — Excludes `patchpilot-backups` from check.
- **Uninstall cleanup Job deleting backup archives** — Added exclusion.
- **`patchpilot-backups` PV stuck `Released` blocking reinstall** — Clears `claimRef`.
- **crictl image cleanup** — Now runs automatically via SSH.

---

## [0.9.5-alpha] — 2026-02-26

### Added
- **Web-based Uninstaller** (Settings → Advanced → Danger Zone).
- **`backend/uninstall_api.py`** — Docker and K3s uninstall support.
- **`PACKAGE:` output in check playbook** — Populates packages table.
- **Phased update support** — Ubuntu phased packages forced through.

### Fixed
- **`become_password` with special characters** — JSON encoding.
- **False-positive "patched" detection** — Requires `changed:` exclusively.
- **Stale apt cache causing check/patch disagreement**.
- **Update Types chart always empty**.

---

## [0.9.4-alpha] — 2026-02-24

### Added
- **File upload for SSH keys in setup wizard**.
- **Hosts created during setup get default key**.
- **`seed-ansible` init container**.

### Fixed
- **Settings → Hosts 500 error on fresh install** — Column name mismatch.
- **Ansible playbooks missing from PVC** — Dockerfile copies `ansible/` to image.
- **SSH key `error in libcrypto`** — Normalize line endings, ensure trailing `\n`.
- **Default SSH key not resolved** — Inventory builder and test connection now resolve defaults.
- **Frontend API calls broken on `localhost`** — All API references use relative `/api` paths.
- **WebSocket URL** — Derives from `window.location`.
- **PVC `storageClassName` immutability error**.

---

## [0.9.3-alpha] — 2026-02-23

### Fixed
- **imagePullPolicy changed to `Always`**.

### Added
- `k8s/nuke-data.sh`.

---

## [0.9.2-alpha] — 2026-02-21

### Added
- **K3s / Kubernetes install path** — full native k3s deployment.
- **`k8s/install-config.yaml`** — single YAML config.
- **`k8s/install-k3s.sh`** — automated k3s installer.
- **`Dockerfile.frontend`** — separate frontend image.
- **Kubernetes manifest templates** (`k8s/templates/00–09`).
- **HSTS and security headers** via Traefik middleware.

---

## [2.0.0] — 2026-02-11

### Added
- Saved SSH Keys Library — store, reuse, upload, set defaults; AES-256 encrypted at rest.
- Real-time WebSocket patching progress.
- Single-host check API.
- Auto-reboot management.
- macOS system update and App Store detection.

---

## [1.0.0] — 2026-01-15

### Added
- Initial release.
- Multi-platform support: Debian/Ubuntu, RHEL/CentOS, macOS.
- Host management, encrypted SSH credential storage, dashboard, settings.

---

**Legend:** Added · Changed · Deprecated · Removed · Fixed · Security
