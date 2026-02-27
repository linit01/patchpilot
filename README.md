<p align="center">
  <img src="frontend/patchpilot-icon.jpeg" alt="PatchPilot" width="48" style="vertical-align:middle"> **PatchPilot**
</p>

# PatchPilot

**Automated patch management system for Linux and macOS hosts — real-time monitoring, secure SSH execution, and a dark-themed web dashboard.**

![Version](https://img.shields.io/badge/version-0.9.4--alpha-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
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

### Security & Authentication
- **Login Required** — Session-based auth before any dashboard access
- **Fernet Encryption (AES-256)** — All SSH private keys and sudo passwords encrypted at rest in PostgreSQL
- **Saved SSH Keys Library** — Store, reuse, upload, and set defaults per host
- **Per-Host SSH Configuration** — Different key, user, and port per target
- **Control Node Protection** — Detects when a managed host is also running PatchPilot; warns before patching, never auto-reboots it

### Infrastructure & Deployment
- **Docker Compose** — Single-command local or LAN deployment
- **K3s / Kubernetes** — Full manifest set with Traefik ingress, cert-manager, Let's Encrypt TLS (DNS-01 Cloudflare or HTTP-01)
- **PostgreSQL 15** — Persistent storage for hosts, packages, SSH keys, settings, schedules, and audit history
- **Ansible** — Remote execution engine; playbook and inventory configurable per deployment

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

---

## Installation

PatchPilot ships with a single installer that supports two deployment modes.

### Prerequisites

**Both modes require on the machine where you run `install.sh`:**

| Requirement | Notes |
|-------------|-------|
| Docker (Desktop or Engine) | Must be running — used to build images |
| Python 3.8+ | For the YAML config parser and key generation |
| Git | To clone the repo |

**Docker Compose mode additionally requires:**
| Requirement | Notes |
|-------------|-------|
| Docker Compose (plugin or legacy) | Ships with Docker Desktop |

**K3s mode additionally requires:**
| Requirement | Notes |
|-------------|-------|
| `kubectl` configured | Pointing at your k3s cluster |
| SSH access to k3s node | When building on macOS or a machine that is not itself a k3s node |
| cert-manager installed | In the cluster |
| Cloudflare API token secret | Pre-created in the `cert-manager` namespace (DNS-01 only) |

### Quick Install

```bash
git clone https://github.com/yourusername/patchpilot.git
cd patchpilot

# Interactive — prompts for install mode
./install.sh

# Or specify directly
./install.sh --docker   # Docker Compose
./install.sh --k3s      # K3s / Kubernetes
```

### Docker Compose (LAN / single host)

The installer handles everything:

1. Generates a Fernet encryption key and writes `.env`
2. Locates or prompts for your Ansible playbook and inventory
3. Builds the backend image and starts all services
4. Accessible at `http://<host-ip>:8080`

To add HTTPS, put a reverse proxy (Nginx Proxy Manager, Traefik, Caddy, Cloudflare Tunnel) in front of port 8080.

### K3s / Kubernetes

See **[KUBERNETES.md](KUBERNETES.md)** for the full step-by-step guide.

```bash
# 1. Edit the config (hostnames, email, storage class, etc.)
nano k8s/install-config.yaml

# 2. Run
./install.sh --k3s

# Preview without applying
./k8s/install-k3s.sh --dry-run

# Uninstall
./k8s/install-k3s.sh --uninstall
```

---

## Configuration

### Environment Variables (Docker Compose — `.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PATCHPILOT_ENCRYPTION_KEY` | ✅ | — | Fernet key (auto-generated by installer) |
| `POSTGRES_USER` | | `patchpilot` | DB username |
| `POSTGRES_PASSWORD` | ✅ | — | DB password |
| `POSTGRES_DB` | | `patchpilot` | DB name |
| `APP_BASE_URL` | | `http://localhost:8080` | Public URL (used for CORS + cookies) |
| `ALLOWED_ORIGINS` | | `*` | Comma-separated CORS origins |
| `AUTO_REFRESH_INTERVAL` | | `300` | Background check interval (seconds) |
| `DEFAULT_SSH_USER` | | `root` | Default SSH user for new hosts |
| `DEFAULT_SSH_PORT` | | `22` | Default SSH port for new hosts |
| `BACKUP_RETAIN_COUNT` | | `10` | Max backups to keep |

### K3s Config File (`k8s/install-config.yaml`)

All k3s settings live in one YAML file — see inline comments for every option. Key sections:

- `patchpilot.network` — hostname(s), TLS, ingress class
- `patchpilot.certManager` — Let's Encrypt email, challenge type, Cloudflare settings
- `patchpilot.postgres` — credentials and storage class
- `patchpilot.app` — encryption key, SSH defaults, backup retention
- `patchpilot.ansible` — playbook and inventory paths

---

## Usage

### Dashboard

| Card | Meaning |
|------|---------|
| Total Hosts | All configured hosts |
| Up to Date | No pending packages |
| Need Updates | Patches available |
| Unreachable | SSH failed on last check |
| Total Pending | Package count across fleet |

### Adding a Host

1. **Settings → Hosts → Add New Host**
2. Fill in hostname/IP, SSH user, port
3. Select a saved SSH key (or enter a password)
4. Click **Test Connection** to verify
5. Save — a background check runs automatically within 30 seconds

### Patching

1. Select one or more hosts with the checkboxes
2. **Patch Selected**
3. Enter the sudo password for those hosts
4. Watch real-time output stream in the progress modal
5. Dashboard refreshes automatically on completion

### SSH Keys

1. **Settings → SSH Keys → Add SSH Key**
2. Paste or upload the private key file
3. Check **Set as default** to auto-select for new hosts
4. Keys are encrypted with your Fernet key before storage in PostgreSQL

### Backup & Restore

See **[README_BACKUP_RESTORE.md](README_BACKUP_RESTORE.md)** for full instructions.

---

## Security

- All SSH private keys and sudo passwords are encrypted at rest (Fernet / AES-256) before being written to PostgreSQL
- Temporary key files are created with `0600` permissions and deleted immediately after use
- The Fernet key lives in an environment variable (`.env` for Docker, Kubernetes Secret for k3s) — never in the database
- Traefik middleware enforces HSTS and standard security headers in k3s mode
- Control node (the host running PatchPilot) is detected automatically and protected from accidental auto-reboot

### Best Practices

1. Use SSH key authentication over passwords
2. Scope Cloudflare API tokens to Zone:DNS:Edit on your domain only
3. Never commit `.env` to version control
4. Run `BACKUP_RETAIN_COUNT` backups and store one off-site
5. Test patching on non-critical hosts first
6. Enable auto-reboot only on hosts that can afford unplanned reboots

---

## API Reference

### Hosts

```
GET    /api/hosts              List all hosts
POST   /api/hosts              Create host
GET    /api/hosts/{id}         Get host
PUT    /api/hosts/{id}         Update host
DELETE /api/hosts/{id}         Delete host
GET    /api/hosts/{id}/packages  Packages for host
```

### Checks & Patching

```
POST /api/check                Full fleet check
POST /api/check/{hostname}     Single-host check
POST /api/patch                Patch selected hosts
GET  /api/stats                Dashboard statistics
```

### SSH Keys

```
GET    /api/settings/ssh-keys
POST   /api/settings/ssh-keys
PUT    /api/settings/ssh-keys/{id}
DELETE /api/settings/ssh-keys/{id}
GET    /api/settings/ssh-keys/{id}/decrypt
```

### WebSocket

```
WS /ws/patch-progress

Message types:
  start     { type, hosts }
  progress  { type, host, message, timestamp }
  success   { type }
  complete  { type }
  error     { type, message }
```

---

## Troubleshooting

### Host shows "Unreachable"

```bash
# Test SSH from the backend container
docker exec -it patchpilot-backend-1 ssh -i /root/.ssh/id_rsa user@host

# k3s
kubectl exec -n patchpilot deploy/patchpilot-backend -- ssh -i /root/.ssh/id_rsa user@host

# Test Ansible
docker exec -it patchpilot-backend-1 ansible all -i /ansible/hosts -m ping
```

### Backend won't start (DB connection failed)

```bash
# Docker
docker compose logs postgres
docker compose logs backend

# k3s
kubectl logs -n patchpilot -l app=patchpilot-postgres
kubectl logs -n patchpilot -l app=patchpilot-backend
```

### Patching fails

```bash
# Docker — tail backend logs during a patch
docker compose logs -f backend

# k3s
kubectl logs -n patchpilot -l app=patchpilot-backend -f
```

Common causes: wrong sudo password, locked package manager (another process), disk full.

### TLS certificate not issuing (k3s)

```bash
kubectl describe cert patchpilot-tls -n patchpilot
kubectl logs -n cert-manager deploy/cert-manager
```

Check the Cloudflare API token has Zone:DNS:Edit permission and the secret is in the `cert-manager` namespace.

### Reset database (⚠️ destroys all data)

```bash
# Docker
docker compose down -v && docker compose up -d

# k3s — delete and recreate the PVC
kubectl delete pvc postgres-data -n patchpilot
kubectl rollout restart deploy/patchpilot-postgres -n patchpilot
```

---

## Project Structure

```
patchpilot/
├── backend/
│   ├── app.py                  # FastAPI application + routes
│   ├── ansible_runner.py       # Ansible execution wrapper
│   ├── database.py             # PostgreSQL (asyncpg) client
│   ├── auth.py                 # Session authentication
│   ├── settings_api.py         # Hosts, SSH keys, general settings
│   ├── schedules_api.py        # Scheduled patch windows
│   ├── backup_restore.py       # Backup / restore logic
│   ├── encryption_utils.py     # Fernet encrypt/decrypt
│   ├── requirements.txt
│   └── migrations/             # SQL migration scripts
├── frontend/
│   ├── index.html              # Main dashboard
│   ├── login.html              # Login page
│   ├── settings.html           # Settings (hosts, keys, general)
│   ├── backup_restore_tab.html # Backup & restore UI
│   ├── app.js                  # Dashboard logic
│   └── styles.css
├── k8s/
│   ├── install-config.yaml     # ← Edit this before k3s install
│   ├── install-k3s.sh          # K3s installer script
│   └── templates/              # Kubernetes manifest templates
├── Dockerfile                  # Backend image
├── Dockerfile.frontend         # Frontend image (nginx + static files)
├── docker-compose.yml          # Docker Compose deployment
├── nginx.conf                  # Nginx config (Docker Compose mode)
├── install.sh                  # Main installer (Docker or K3s)
├── database-schema.sql         # Initial schema
└── .env.example                # Environment variable template
```

---

*Built for sysadmins who patch first and ask questions never.*
