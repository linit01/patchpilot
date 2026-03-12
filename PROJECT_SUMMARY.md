# PatchPilot — Project Summary

**Version:** 0.10.0-alpha
**Status:** Active development — approaching public release

---

## What It Is

PatchPilot is a self-hosted patch management dashboard for Linux and macOS systems. It monitors update status across your fleet, runs patching via Ansible, and provides a dark-themed web UI with real-time progress streaming. It includes built-in backup/restore, scheduled patching, and one-click application updates.

## Deployment Modes

| Mode | Command | Access |
|------|---------|--------|
| Docker Compose | `./install.sh --docker` | `http://<host>:8080` |
| K3s / Kubernetes | `./install.sh --k3s` | `https://<hostname>` (TLS via cert-manager) |
| Web Installer | `./install.sh --web` | Interactive browser-based setup |

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Frontend | HTML5 · Vanilla JS · WebSocket API |
| Backend | Python 3.11 · FastAPI · Uvicorn |
| Database | PostgreSQL 15 (asyncpg) |
| Remote execution | Ansible (inside backend container) |
| Encryption | cryptography (Fernet / AES-256) |
| Web server | Nginx (Alpine) |
| Container runtime | Docker / containerd (k3s) |
| Ingress (k3s) | Traefik v3 |
| TLS (k3s) | cert-manager + Let's Encrypt |
| CI/CD | GitHub Actions · Docker Hub (multi-arch: amd64 + arm64) |

## Key Features

- **Multi-platform** — Debian/Ubuntu (`apt`), RHEL/CentOS (`dnf`/`yum`), macOS (`brew` + `softwareupdate` + `mas`)
- **Encrypted credentials** — SSH keys and sudo passwords encrypted with Fernet (AES-256) before PostgreSQL storage
- **Saved SSH Keys Library** — store, reuse, upload, set defaults; auto-assigned to new hosts
- **Real-time patching** — WebSocket streaming of live Ansible output
- **Background checks** — configurable interval (default 5 min) with countdown timer
- **Scheduled patching** — time-based patch windows
- **In-app updates** — automatic GitHub release checking with one-click updates for both Docker and Kubernetes
- **Backup & restore** — full application backup with optional encryption key export and retention protection
- **Setup wizard** — first-run wizard covering admin account, settings, backup storage, and default SSH key
- **In-app uninstall** — web-based uninstaller for both Docker Compose and Kubernetes deployments
- **Control node protection** — detects when a managed host is also running PatchPilot; never auto-reboots it

## Project Structure

```
patchpilot/
├── backend/
│   ├── app.py                  # FastAPI app + startup migrations
│   ├── ansible_runner.py       # Ansible execution + dynamic inventory
│   ├── database.py             # PostgreSQL (asyncpg) client
│   ├── auth.py                 # Session authentication + RBAC
│   ├── settings_api.py         # Hosts, SSH keys, test connection, general settings
│   ├── setup_api.py            # First-run setup wizard API
│   ├── schedules_api.py        # Scheduled patch windows
│   ├── backup_restore.py       # Backup / restore logic
│   ├── uninstall_api.py        # In-app uninstall (Docker + k8s)
│   ├── update_checker.py       # GitHub release checker + update execution
│   ├── encryption_utils.py     # Fernet encrypt/decrypt helpers
│   └── requirements.txt
├── frontend/
│   ├── index.html              # Main dashboard
│   ├── login.html              # Login page
│   ├── setup.html              # First-run setup wizard
│   ├── settings.html           # Settings (hosts, keys, schedules, backup, updates)
│   ├── app.js                  # Dashboard logic + WebSocket client
│   └── styles.css
├── k8s/
│   ├── install-config.yaml     # ← Edit before k3s install
│   ├── install-k3s.sh          # K3s installer
│   ├── build-push.sh           # Build + push images to Docker Hub
│   └── templates/              # Kubernetes manifest templates (00–09)
├── webinstall/                 # Web-based installer UI
├── scripts/
│   ├── push_new_build.sh       # Release tagging helper
│   └── claude-context.sh       # Codebase export for AI sessions
├── .github/workflows/
│   └── docker-build-push.yml   # CI: multi-arch build + auto GitHub Release
├── Dockerfile                  # Backend image
├── Dockerfile.frontend         # Frontend image (nginx + static files)
├── docker-compose.yml          # Docker Compose deployment
├── docker-compose.developer.yml # Developer override (local builds)
├── nginx.conf                  # Nginx config for Docker Compose mode
├── install.sh                  # Main installer (Docker, K3s, or Web)
├── VERSION                     # Current version string
└── database-schema.sql
```

## Roadmap

- [ ] Email / Slack / webhook notifications on patch completion or failures
- [ ] Prometheus metrics endpoint + Grafana dashboard
- [ ] Package-level selection (patch individual packages, not whole host)
- [ ] Rollback support (revert to previous version)
- [ ] RHEL subscription-manager support
- [ ] Helm chart for easier k3s deployment
- [ ] Multi-user RBAC
