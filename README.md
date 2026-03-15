# Designed, built and owned by John R. Sanborn, 2026.
# Code and design advice by Claude.AI and Ollama (local using multiple LLMs).
# UI designs inspired by PiHole app, theme Star Trek LCARS. pi-hole.net, DAN SCHAPER

# contact@getpatchpilot.app
# Git repo: linit01/patchpilot
# Docker hub: linit01/patchpilot
# All rights to this code and design ideas are reserved by owner.

# <img src="frontend/patchpilot-icon.jpeg" alt="" width="40"> PatchPilot

**Automated patch management system for Linux and macOS hosts — real-time monitoring, secure SSH execution, and a dark-themed web dashboard.**

![Version](https://img.shields.io/badge/version-0.10.0--alpha-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Updating](#updating)
- [Security](#security)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)

---

## Features

### Core Functionality
- **Multi-Platform Host Support** — Debian/Ubuntu (`apt`), RHEL/CentOS (`dnf`/`yum`), macOS (`brew` + `softwareupdate` + Mac App Store via `mas`)
- **Real-Time Patching Progress** — WebSocket streaming of live Ansible task output, per-task timestamps
- **Background Checks** — Configurable interval (default 5 min), with countdown timer in the UI
- **Single-Host Checks** — Fast targeted scan (~30 s) via `/api/check/{hostname}` — auto-triggered on host creation
- **Scheduled Patching** — Time-based patch windows with encrypted sudo-password storage
- **In-App Updates** — Automatic update checking with one-click upgrades for both Docker Compose and Kubernetes deployments

### Security & Authentication
- **Login Required** — Session-based auth before any dashboard access
- **Fernet Encryption (AES-256)** — All SSH private keys and sudo passwords encrypted at rest in PostgreSQL
- **Saved SSH Keys Library** — Store, reuse, upload, and set defaults per host
- **Per-Host SSH Configuration** — Different key, user, and port per target
- **Control Node Protection** — Detects when a managed host is also running PatchPilot; warns before patching, never auto-reboots it

### Infrastructure & Deployment
- **Docker Compose** — Single-command local or LAN deployment
- **K3s / Kubernetes** — Full manifest set with Traefik ingress, cert-manager, Let's Encrypt TLS (DNS-01 Cloudflare or HTTP-01)
- **CI/CD** — GitHub Actions builds multi-arch images (amd64 + arm64) and auto-creates GitHub Releases on tag push
- **PostgreSQL 15** — Persistent storage for hosts, packages, SSH keys, settings, schedules, and audit history
- **Ansible** — Remote execution engine; playbook and inventory configurable per deployment

### Backup & Restore
- **Full application backup** — Database, settings, Ansible files, and optionally the encryption key
- **Standalone encryption key export** — Key file written alongside the backup tarball for easy retrieval
- **Retention protection** — Backups containing the encryption key are never deleted by retention policy
- **Web UI and CLI** — Create, download, upload, and restore backups from Settings → Backup & Restore

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
| Git | To clone the repo |

### Quick Install

```bash
git clone https://github.com/linit01/patchpilot.git
cd patchpilot

# Interactive web-based installer (recommended)
./install.sh --web

# Or specify directly
./install.sh --docker   # Docker Compose
./install.sh --k3s      # K3s / Kubernetes
```

### Docker Compose

The installer generates a Fernet encryption key, writes `.env`, pulls pre-built images from Docker Hub, and starts all services. Accessible at `http://<host-ip>:8080`.

### K3s / Kubernetes

See **[KUBERNETES.md](KUBERNETES.md)** for the full step-by-step guide.

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

The backend periodically checks GitHub Releases for new versions (default: every 24 hours). When an update is available, a badge appears on the sidebar and Settings → Updates shows the available version with release notes.

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
POST /api/backup/create        Create backup
POST /api/backup/restore/{f}   Restore from backup
GET  /api/backup/health        Backup health info
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
│   ├── app.py, ansible_runner.py, database.py, auth.py
│   ├── settings_api.py, schedules_api.py, setup_api.py
│   ├── backup_restore.py, uninstall_api.py, update_checker.py
│   └── encryption_utils.py, requirements.txt
├── frontend/                   # Static HTML/JS/CSS dashboard
├── k8s/                        # Kubernetes manifests + installer
├── webinstall/                 # Web-based installer UI
├── scripts/                    # Helper scripts
├── .github/workflows/          # CI/CD pipeline
├── Dockerfile, Dockerfile.frontend
├── docker-compose.yml, docker-compose.developer.yml
├── install.sh, nginx.conf, VERSION
└── database-schema.sql
```

---

*Built for sysadmins who patch first and ask questions never.*
