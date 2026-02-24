# Changelog

All notable changes to PatchPilot will be documented in this file.

---

## [0.9.5-alpha] - 2026-02-24

### Added
- **Web-based Uninstaller (Settings → Advanced → Danger Zone)** — Admin users can now initiate a full PatchPilot uninstall directly from the UI. The system auto-detects the install type (Docker Compose or Kubernetes/k3s) and presents a two-phase workflow:
  - **Preview phase** — shows exactly which steps will be automated vs which require manual host access, before anything is executed.
  - **Confirmation gate** — operator must type `UNINSTALL` to proceed, preventing accidental execution.
  - **Execution phase** — animated progress bar while the backend runs; shows completed/failed steps on completion.
  - **Manual commands panel** — any steps the backend cannot perform (hostPath cleanup, removing the repo directory, optional full k3s/Docker removal) are displayed in a copyable code block.
- **`backend/uninstall_api.py`** — New FastAPI router (`/api/uninstall/status`, `/api/uninstall/execute`). Admin-only. Detects install type via env var `PATCHPILOT_INSTALL_MODE`, k3s kubeconfig presence, or Docker socket. Docker uninstall runs `docker compose down -v`, prunes patchpilot images/volumes. K3s uninstall delegates to existing `k8s/install-k3s.sh --uninstall` (with `NO_INTERACTIVE=true`) or direct `kubectl delete namespace` fallback.
- **Docker uninstall script support** — Docker Compose installs previously had no equivalent to `k8s/install-k3s.sh --uninstall`. The new API handles Docker teardown natively.

### Changed
- `backend/app.py` — registers the new `uninstall_router`.
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
- **SSH key `error in libcrypto` on all key paths** — OpenSSH requires private key files to end with `\n`. All three temp-file write sites (test connection + two Ansible inventory paths) now normalize CRLF line endings and append `\n` if missing, fixing failures caused by browsers stripping the trailing newline from pasted keys.
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
