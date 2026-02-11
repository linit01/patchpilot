# 🎯 PatchPilot

**Automated patch management system for Linux and macOS hosts with real-time monitoring and secure execution.**

![Version](https://img.shields.io/badge/version-2.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## 📋 Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## ✨ Features

### Core Functionality
- **Multi-Platform Support**: Manages updates for Debian/Ubuntu (apt), macOS (Homebrew), and RHEL-based systems
- **Real-Time Monitoring**: WebSocket-powered live patching progress with streaming task output
- **Automated Scheduling**: Background checks every 2 minutes with countdown timer
- **Single-Host Checks**: Fast targeted checks (~30 seconds) instead of full fleet scans

### Security & Authentication
- **Encrypted SSH Key Storage**: AES-256 encryption with Fernet for all credentials
- **Saved SSH Keys Library**: Store and reuse SSH keys across multiple hosts
- **Per-Host SSH Keys**: Support for different keys per host or shared keys
- **Control Node Protection**: Prevents accidental patching of the management server
- **Password Authentication**: Fallback option (not recommended)

### Advanced Features
- **Auto-Reboot Management**: Per-host configurable automatic reboots after patching
- **Ubuntu Phased Updates**: Automatically force-install deferred updates
- **Package-Level Details**: View specific packages pending update with version info
- **SSH ControlMaster Disabled**: Prevents connection pooling conflicts
- **Smart Caching**: Browser cache-busting for real-time UI updates

### User Experience
- **Intuitive Dashboard**: At-a-glance view of fleet health with color-coded status
- **Settings Management**: Centralized host configuration with test connections
- **File Upload Support**: Upload SSH keys instead of copy/paste
- **Default Keys**: Set default SSH key for quick host additions
- **Responsive Design**: Works on desktop and mobile

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend (Nginx)                        │
│  - HTML/CSS/JavaScript                                       │
│  - Real-time WebSocket connection                            │
│  - Browser-based UI                                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Backend (FastAPI/Python)                   │
│  - REST API endpoints                                        │
│  - WebSocket server for real-time updates                   │
│  - Encryption/decryption layer                               │
│  - Background task scheduler                                 │
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
        ┌──────────────────┐  ┌──────────────────┐
        │   PostgreSQL     │  │  Ansible Runner  │
        │   - Hosts        │  │  - SSH executor  │
        │   - Packages     │  │  - Playbooks     │
        │   - SSH Keys     │  │  - Inventory     │
        └──────────────────┘  └──────────────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │  Managed Hosts   │
                              │  - Linux/macOS   │
                              │  - SSH access    │
                              └──────────────────┘
```

### Technology Stack

**Frontend:**
- HTML5/CSS3
- Vanilla JavaScript (no frameworks)
- WebSocket API for real-time updates

**Backend:**
- FastAPI (Python 3.11+)
- asyncpg for PostgreSQL
- Ansible for remote execution
- Cryptography (Fernet) for encryption

**Infrastructure:**
- Docker Compose
- Nginx (frontend server)
- PostgreSQL 15
- Ansible 2.19

## 🚀 Installation

### Prerequisites

- Docker & Docker Compose
- Git
- 2GB RAM minimum
- Linux or macOS host

### Quick Install

```bash
# Clone repository
git clone https://github.com/yourusername/patchpilot.git
cd patchpilot

# Generate encryption key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Create .env file
cat > .env << EOF
ENCRYPTION_KEY=<your-generated-key>
DATABASE_URL=postgresql://patchpilot:patchpilot@postgres:5432/patchpilot
EOF

# Start services
docker-compose up -d

# Check status
docker-compose ps
```

**Access UI:** http://localhost:3000

## 🎬 Quick Start

### 1. Add SSH Key (Recommended)

1. Go to **Settings → 🔑 SSH Keys**
2. Click **"Add SSH Key"**
3. Name: `Personal Laptop`
4. Click **"📁 Choose Key File"** → Select `~/.ssh/id_ed25519`
5. Check **"Set as default key"**
6. Click **"Save Key"**

### 2. Add Your First Host

1. Go to **Settings → 📋 Hosts**
2. Click **"Add New Host"**
3. Fill in:
   - **Hostname:** `server.example.com` or IP address
   - **SSH User:** `your-username`
   - **SSH Port:** `22` (default)
   - **SSH Authentication:** Select your saved key (auto-selected if default)
4. Click **"Test Connection"** (optional)
5. Click **"Save Host"**

**Within 30 seconds**, the host will be checked and appear in the dashboard!

### 3. Patch Hosts

1. Select hosts with pending updates
2. Click **"⚡ Patch Selected"**
3. Enter sudo password
4. Click **"Confirm Patch"**
5. Watch real-time progress in the modal

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ENCRYPTION_KEY` | Fernet encryption key (required) | None |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://...` |
| `ANSIBLE_HOST_KEY_CHECKING` | Disable SSH host key checking | `False` |
| `ANSIBLE_SSH_ARGS` | SSH connection options | ControlMaster disabled |

### Per-Host Settings

**Auto-Reboot:**
- Click host → **View Details**
- Enable **"Allow Auto-Reboot"**
- Host will reboot automatically after patching if required

**Control Node:**
- Automatically detected (host running PatchPilot)
- Orange warning badge in dashboard
- Requires confirmation before patching
- Never auto-reboots (safety feature)

### Background Checks

- Runs every **2 minutes** automatically
- Can be triggered manually via **"Refresh Status"**
- Countdown timer shows next check
- Single-host checks available via API

## 📖 Usage

### Dashboard

**Stats Cards:**
- **Total Hosts:** Count of configured hosts
- **Up to Date:** Hosts with no pending updates
- **Need Updates:** Hosts with patches available
- **Unreachable:** Hosts that can't be reached via SSH
- **Total Pending Updates:** Aggregate package count

**Host Table:**
- Checkbox for bulk operations
- Color-coded status badges
- Last checked timestamp
- View Details button for package list

### Settings Tabs

**📋 Hosts:**
- List all configured hosts
- Add/Edit/Delete hosts
- Test SSH connections
- Import/Export configurations

**🔑 SSH Keys:**
- Manage saved SSH keys
- Set default key
- Encrypted storage
- Reuse across hosts

**⚙️ General:**
- Application settings
- Future configuration options

**🔧 Advanced:**
- System information
- Ansible version
- Debug options

## 🔒 Security

### Encryption

All sensitive data is encrypted at rest:
- SSH private keys → AES-256 (Fernet)
- SSH passwords → AES-256 (Fernet)
- Encryption key stored in environment variable

### SSH Security

- Keys stored encrypted in PostgreSQL BYTEA columns
- Temporary key files created with 0600 permissions
- Keys decrypted only when needed
- Temp files cleaned up after use
- No keys logged or exposed in UI

### Network Security

**Allowed Outbound Domains:**
- `api.anthropic.com`
- `*.ubuntu.com`
- `*.pythonhosted.org`
- `registry.npmjs.org`
- And other package repositories

**No Inbound Access Required:**
- PatchPilot connects outbound to managed hosts
- Hosts don't need to reach PatchPilot

### Best Practices

1. **Use SSH Keys** over passwords
2. **Rotate encryption key** periodically
3. **Limit sudo access** on managed hosts
4. **Review logs** regularly
5. **Test on dev hosts** before production
6. **Enable auto-reboot** only on non-critical hosts
7. **Backup database** with encrypted credentials

## 🛠 Development

### Project Structure

```
patchpilot/
├── backend/
│   ├── app.py                 # FastAPI main application
│   ├── ansible_runner.py      # Ansible execution wrapper
│   ├── database.py            # Database client
│   ├── settings_api.py        # Settings endpoints
│   ├── encryption_utils.py    # Encryption/decryption
│   ├── crypto_utils.py        # Key management
│   └── migrations/            # SQL migrations
├── frontend/
│   ├── index.html            # Main dashboard
│   ├── settings.html         # Settings interface
│   ├── app.js                # Dashboard logic
│   └── styles.css            # Global styles
├── ansible/
│   ├── check-os-updates.yml  # Update check playbook
│   └── hosts                 # Static inventory (fallback)
└── docker-compose.yml        # Service orchestration
```

### Running Tests

```bash
# Backend tests
docker exec patchpilot-backend pytest

# Check logs
docker-compose logs -f backend

# Database access
docker exec -it patchpilot-db psql -U patchpilot -d patchpilot
```

### Adding Features

1. **Backend:** Add endpoint to `settings_api.py`
2. **Database:** Create migration in `migrations/`
3. **Frontend:** Update `settings.html` or `index.html`
4. **Test:** Manually test all workflows

### Debug Mode

Enable debug logging:

```python
# backend/app.py
import logging
logging.basicConfig(level=logging.DEBUG)
```

## 🐛 Troubleshooting

### Host Shows "Unreachable"

**Check:**
1. SSH key is correct: `ssh -i ~/.ssh/id_ed25519 user@host`
2. Host is online: `ping host`
3. SSH port is accessible: `nc -zv host 22`
4. Firewall allows outbound SSH from PatchPilot
5. Backend logs: `docker-compose logs backend | grep hostname`

### Patching Fails

**Common causes:**
1. **Permission denied:** User needs sudo without password or provide sudo password
2. **Locked package manager:** Another process is using apt/brew
3. **Network timeout:** Poor connection or slow mirrors
4. **Disk full:** No space for package downloads

**Check logs:**
```bash
docker-compose logs backend --tail 100 | grep -A 20 "Running Ansible patch"
```

### Auto-Reboot Not Working

**Requirements:**
1. Host has `/var/run/reboot-required` file (Ubuntu/Debian)
2. "Allow Auto-Reboot" is enabled for host
3. Host is NOT the control node
4. Patch completed successfully

### Database Connection Fails

```bash
# Check PostgreSQL status
docker-compose ps postgres

# View logs
docker-compose logs postgres

# Reset database (⚠️ destroys data)
docker-compose down -v
docker-compose up -d
```

### Frontend Not Loading

```bash
# Check Nginx logs
docker-compose logs frontend

# Verify container is running
docker-compose ps frontend

# Check file permissions
docker exec patchpilot-frontend ls -la /usr/share/nginx/html/
```

## 📝 API Documentation

### REST Endpoints

```
GET  /api/hosts              # List all hosts
POST /api/hosts              # Create host
GET  /api/hosts/{id}         # Get host details
PUT  /api/hosts/{id}         # Update host
DELETE /api/hosts/{id}       # Delete host

GET  /api/hosts/{id}/packages  # List packages for host

POST /api/check              # Trigger full fleet check
POST /api/check/{hostname}   # Check single host

POST /api/patch              # Patch selected hosts
GET  /api/stats              # Dashboard statistics

GET  /api/settings/ssh-keys        # List saved SSH keys
POST /api/settings/ssh-keys        # Create SSH key
GET  /api/settings/ssh-keys/{id}   # Get SSH key metadata
PUT  /api/settings/ssh-keys/{id}   # Update SSH key
DELETE /api/settings/ssh-keys/{id} # Delete SSH key
GET  /api/settings/ssh-keys/{id}/decrypt  # Get decrypted key content
```

### WebSocket

```
WS /ws/patch-progress        # Real-time patching updates

Message types:
- start: { type: "start", hosts: [...] }
- progress: { type: "progress", host: "...", message: "..." }
- success: { type: "success" }
- complete: { type: "complete" }
- error: { type: "error", message: "..." }
```

## 📄 License

MIT License - See LICENSE file for details

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Test thoroughly
4. Submit pull request

## 📧 Support

- **Issues:** https://github.com/yourusername/patchpilot/issues
- **Discussions:** https://github.com/yourusername/patchpilot/discussions

---

**Built with ❤️ for sysadmins who value automation and security**
