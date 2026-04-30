# PatchPilot — Patch Management for Linux & macOS

# https://github.com/linit01/patchpilot

# <img src="frontend/patchpilot-icon.jpeg" alt="" width="40"> PatchPilot

**Automated patch management system for Linux and macOS hosts — real-time monitoring, secure SSH execution, and a dark-themed web dashboard.**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-Proprietary-blue)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Licensing](#licensing)
- [Configuration](#configuration)
- [Usage](#usage)
- [Updating](#updating)
- [Security](#security)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)

---

## Features

### Core Functionality
- **Multi-Platform Host Support** — Debian/Ubuntu (`apt`), RHEL/CentOS (`dnf`/`yum`), macOS (`brew` + `softwareupdate` + Mac App Store via `mas`), Windows (`winget` + PSWindowsUpdate)
- **Real-Time Patching Progress** — WebSocket streaming of live Ansible task output, per-task timestamps
- **Background Checks** — Configurable interval (default 5 min), with countdown timer in the UI
- **Single-Host Checks** — Fast targeted scan (~30 s) via `/api/check/{hostname}` — auto-triggered on host creation
- **Scheduled Patching** — Time-based patch windows with encrypted sudo-password storage
- **In-App Updates** — Automatic update checking with one-click upgrades for both Docker Compose and Kubernetes deployments
- **Native iOS App** — SwiftUI app at `patchpilot/ios/`; dashboard, host list, patch operations with real-time WebSocket progress, Bearer token auth, Keychain storage; distribute via TestFlight

### Package Exclusions
- **MAS exclusions** — App Store numeric IDs displayed in Pending Packages with copy button; add to Settings → macOS to skip during patching
- **Winget exclusions** — `Package.Id` displayed in Pending Packages with copy button; add to Settings → Windows to skip
- **macOS system update exclusions** — `softwareupdate` label prefixes shown as Exclusion ID; add to Settings → macOS to skip specific items (e.g. `Command Line Tools for Xcode`) while still applying others

### Security & Authentication
- **Multi-User RBAC** — Three-tier role model: Full Admin (app owner, sees/manages all), Admin (own resources only), Viewer (read-only across all resources)
- **Resource Ownership** — Hosts, SSH keys, and schedules tracked by creator; Admin users see only their own resources
- **Login Required** — Session-based auth before any dashboard access
- **Fernet Encryption (AES-256)** — All SSH private keys and sudo passwords encrypted at rest in PostgreSQL
- **Saved SSH Keys Library** — Store, reuse, upload, and set defaults per host; scoped per user
- **Per-Host SSH Configuration** — Different key, user, and port per target
- **Control Node Protection** — Detects when a managed host is also running PatchPilot; warns before patching, never auto-reboots it
- **Debug Logging Toggle** — Runtime-switchable verbose logging via Settings → Advanced (no restart needed)

### Infrastructure & Deployment
- **Docker Compose** — Single-command local or LAN deployment
- **K3s / Kubernetes** — Full manifest set with Traefik ingress, cert-manager, Let's Encrypt TLS (DNS-01 Cloudflare or HTTP-01)
- **CI/CD** — GitHub Actions builds multi-arch images (amd64 + arm64) and auto-creates GitHub Releases on tag push
- **PostgreSQL 15** — Persistent storage for hosts, packages, SSH keys, settings, schedules, and audit history
- **Ansible** — Remote execution engine; playbook and inventory configurable per deployment

### Backup & Restore
- **Full application backup** — Database, settings, Ansible files, and optionally the encryption key
- **Standalone encryption key export** — Key file written alongside the backup tarball for easy retrieval
- **Smart retention policy** — Configurable retain count; uninstall backups excluded from count; companion key files cleaned up; at least one encryption-key-bearing backup preserved
- **Web UI and CLI** — Create, download, upload (.tgz and .tar.gz), and restore backups from Settings → Backup & Restore
- **License-gated** — Backup features require an active license (trial users see a lock overlay)

### Licensing & Trial
- **14-day free trial** — Starts automatically on first-run setup; full functionality except backup/restore
- **LemonSqueezy integration** — License keys validated via LemonSqueezy License API with machine binding
- **Activation limit** — Each key activates on one installation; deactivate to move to a new machine
- **Periodic validation** — Background check every 7 days confirms license status with LemonSqueezy
- **30-day grace period** — Offline installations continue working for 30 days between validations
- **Subscription management** — Expiry, cancellation, and admin-disabled states detected automatically

---

## Architecture

```
Browser (HTTPS / HTTP)
        │
        ▼
  Traefik Ingress ─────────────────────────── (k3s only)
  or Nginx (Docker)
        │
        ▼
  patchpilot-frontend  (Nginx serving static HTML/JS/CSS)
        │
        │  /api/*  and  /ws/*
        ▼
  patchpilot-backend   (Python 3.11 · FastAPI · Uvicorn)
        │
   ┌────┴────┐
   │         │
   ▼         ▼
PostgreSQL  Ansible Runner
(port 5432) (SSH → managed hosts)
```

### Technology Stack

| Layer | Technology |
|-------|-----------|
| Frontend | HTML5 · Vanilla JS · WebSocket API |
| Backend | Python 3.11 · FastAPI · Uvicorn |
| Database | PostgreSQL 15 |
| Remote execution | Ansible (inside backend container) |
| Encryption | `cryptography` (Fernet / AES-256) |
| Web server | Nginx (Alpine) |
| Container runtime | Docker / containerd (k3s) |
| Ingress (k3s) | Traefik v3 |
| TLS (k3s) | cert-manager + Let's Encrypt |
| CI/CD | GitHub Actions · Docker Hub |

---

## Installation

PatchPilot ships with a single installer that supports multiple deployment modes.

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker (Desktop or Engine) | Must be running |
| Python 3.8+ | For config parsing and key generation |
| Git | To clone the repo (or use the curl installer) |

### Quick Install

```bash
# One-line installer (clone or tarball — auto-detected): bootstrap, Docker Compose, pull images
curl -fsSL https://getpatchpilot.app/install.sh | bash

# Or clone and run manually
git clone https://github.com/linit01/patchpilot.git
cd patchpilot
./install.sh              # Web wizard (default)
./install.sh --docker     # Docker Compose (pull published images)
./install.sh --docker --developer   # Docker Compose — build images from local source (contributors)
./install.sh --k3s        # K3s / Kubernetes
```

After the containers are up, open the dashboard and complete **first-run setup** in the browser (`setup.html`).  
Visit **[getpatchpilot.app](https://getpatchpilot.app)** for screenshots and full details.

### Docker Compose

The installer generates a Fernet encryption key, writes `.env`, **pulls** pre-built images from Docker Hub (or **builds** from source when using `--docker --developer`), and starts all services. Accessible at `http://<host-ip>:8080`. On Linux, use a non-root account that can run Docker (e.g. member of the `docker` group).

### K3s / Kubernetes

See **[KUBERNETES.md](KUBERNETES.md)** for the full step-by-step guide.

---

## Licensing

PatchPilot includes a **14-day free trial** with full functionality (except backup/restore). After the trial, a license key is required.

### Pricing

| Plan | Price |
|------|-------|
| Monthly | $5.99/month |
| Annual | $49/year (save 32%) |

Purchase at **[getpatchpilot.app](https://getpatchpilot.app)** or via the PatchPilot store at `patchpilot.lemonsqueezy.com`.

### Activating a License

1. Purchase a subscription — you'll receive a license key (UUID format)
2. Open **Settings → License** in PatchPilot
3. Enter the key and click **Activate**
4. PatchPilot validates the key with LemonSqueezy and binds it to your installation

Each key activates on **one installation**. To move your license to a new machine, deactivate it first in Settings → License, then re-activate on the new installation.

---

## Configuration

### Environment Variables (Docker Compose — `.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PATCHPILOT_ENCRYPTION_KEY` | ✅ | — | Fernet key (auto-generated by installer) |
| `POSTGRES_USER` | | `patchpilot` | DB username |
| `POSTGRES_PASSWORD` | ✅ | — | DB password |
| `POSTGRES_DB` | | `patchpilot` | DB name |
| `APP_BASE_URL` | | `http://localhost:8080` | Public URL (CORS + cookies) |
| `ALLOWED_ORIGINS` | | `*` | Comma-separated CORS origins |
| `AUTO_REFRESH_INTERVAL` | | `300` | Background check interval (seconds) |
| `BACKUP_RETAIN_COUNT` | | `10` | Max backups to keep |
| `INSTALL_DIR` | | — | Host path of PatchPilot directory (set by installer) |

---

## Usage

### Dashboard

Stats cards show hosts up to date, needing updates, unreachable, and total pending packages. Host table with status badges and last-checked timestamps. View Details for per-host package lists.

### Adding a Host

Settings → Hosts → Add New Host. Fill in hostname/IP, SSH user, port, select a saved SSH key, click Test Connection, then Save. A background check runs automatically within 30 seconds.

### Patching

Select hosts → Patch Selected → enter sudo password → watch real-time progress → dashboard auto-refreshes on completion.

### Backup & Restore

See **[README_BACKUP_RESTORE.md](README_BACKUP_RESTORE.md)** for full instructions.

---

## Updating

PatchPilot includes a built-in update checker and one-click update mechanism.

### Automatic Checks

The backend periodically checks for new versions (default: every 24 hours). It queries GitHub Releases first; if the repo is private and no token is configured, it falls back to Docker Hub tags. When an update is available, a badge appears on the sidebar and Settings → Updates shows the available version with release notes.

### One-Click Updates

Click **Update Now** in Settings → Updates. For Kubernetes, the backend updates deployment image tags and issues a rollout restart. For Docker Compose, it rewrites `docker-compose.yml` tags, pulls new images, and spawns a helper container to restart services. The frontend auto-reconnects and reloads.

### Manual Updates

```bash
# Docker Compose
docker compose pull && docker compose up -d

# Kubernetes
kubectl -n patchpilot set image deployment/patchpilot-backend backend=linit01/patchpilot:backend-<version>
kubectl -n patchpilot set image deployment/patchpilot-frontend frontend=linit01/patchpilot:frontend-<version>
kubectl -n patchpilot rollout restart deployment/patchpilot-backend deployment/patchpilot-frontend
```

---

## Security

- All SSH private keys and sudo passwords encrypted at rest (Fernet / AES-256)
- Temporary key files created with `0600` permissions and deleted after use
- Fernet key stored in environment variable — never in the database
- Traefik middleware enforces HSTS and security headers in k3s mode
- Control node protected from accidental auto-reboot

---

## API Reference

### Hosts
```
GET/POST   /api/hosts              List / Create
GET/PUT/DELETE /api/hosts/{id}     Get / Update / Delete
```

### Checks & Patching
```
POST /api/check                Full fleet check
POST /api/check/{hostname}     Single-host check
POST /api/patch                Patch selected hosts
GET  /api/stats                Dashboard statistics
```

### Updates
```
GET  /api/updates/status       Update status (cached)
POST /api/updates/check        Force check
POST /api/updates/apply        Apply update
GET  /api/updates/progress     Poll progress
```

### Backup & Restore
```
GET  /api/backup/list          List backups
POST /api/backup/create        Create backup (license required)
POST /api/backup/restore/{f}   Restore from backup (license required)
GET  /api/backup/health        Backup health info
```

### License
```
GET  /api/license/status       Trial/license status
POST /api/license/activate     Activate license key (LemonSqueezy)
POST /api/license/deactivate   Deactivate and free activation slot
POST /api/license/validate     Manual re-validation
```

### WebSocket
```
WS /ws/patch-progress
```

---

## Troubleshooting

### Host shows "Unreachable"
Test SSH from the backend container: `docker exec -it patchpilot-backend-1 ssh user@host`

### Backend won't start
Check logs: `docker compose logs backend` or `kubectl logs -n patchpilot deploy/patchpilot-backend -c backend`

### TLS not issuing (k3s)
`kubectl describe cert patchpilot-tls -n patchpilot`

---

## Project Structure

```
patchpilot/
├── backend/                    # FastAPI application
│   ├── app.py, ansible_runner.py, database.py, auth.py, rbac.py
│   ├── settings_api.py, schedules_api.py, setup_api.py
│   ├── backup_restore.py, uninstall_api.py, update_checker.py
│   ├── license.py              # Trial/license management (LemonSqueezy)
│   └── encryption_utils.py, requirements.txt
├── frontend/                   # Static HTML/JS/CSS dashboard
├── k8s/                        # Kubernetes manifests + installer
├── webinstall/                 # Web-based installer UI
├── scripts/                    # Helper scripts
├── .github/workflows/          # CI/CD pipeline
├── Dockerfile, Dockerfile.frontend
├── docker-compose.yml, docker-compose.developer.yml
├── install.sh, nginx.conf, VERSION, LICENSE
└── database-schema.sql
```

---

*Built for sysadmins who patch first and ask questions never.*
