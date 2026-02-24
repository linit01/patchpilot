# PatchPilot — Quick Start Guide

Get PatchPilot running in **under 10 minutes**.

---

## Choose Your Install Mode

| Mode | Best for | Time |
|------|----------|------|
| Docker Compose | Single host, home lab, LAN access | ~5 min |
| K3s / Kubernetes | Cluster deployment, HTTPS, production-grade | ~10 min |

---

## Option A — Docker Compose

### What you need

- **Docker** (Docker Desktop on Mac/Windows, Docker Engine on Linux) — must be **running**
- **Docker Compose** (bundled with Docker Desktop, or install the plugin)
- Your Ansible playbook (`check-os-updates.yml`) and inventory (`hosts` file)

### Install

```bash
git clone https://github.com/yourusername/patchpilot.git
cd patchpilot
./install.sh --docker
```

The installer will:
1. Verify Docker is installed and running
2. Generate a Fernet encryption key and write `.env`
3. Find (or prompt for) your Ansible playbook and inventory
4. Build the backend image
5. Start PostgreSQL, backend, and frontend containers

**Dashboard:** `http://localhost:8080`

### Managing services

```bash
# Stream all logs
docker compose logs -f

# Stream backend only
docker compose logs -f backend

# Stop
docker compose down

# Restart
docker compose restart

# Upgrade (after git pull)
docker compose up -d --build
```

### Access from other LAN devices

PatchPilot binds to `0.0.0.0:8080` by default, so any device on your LAN can reach it at `http://<host-ip>:8080`.

To add HTTPS, put a reverse proxy in front (Nginx Proxy Manager, Traefik, Caddy, or a Cloudflare Tunnel) pointing at port 8080.

---

## Option B — K3s / Kubernetes

### What you need

On the machine where you run the installer (your Mac or Linux workstation):

- **Docker** — must be installed and **running** (used to build images here, not on the cluster)
- **`kubectl`** — configured and pointing at your k3s cluster
- **Docker Hub account** — `linit01/patchpilot` private repo (username + access token)
- **Python 3** with PyYAML (`pip3 install pyyaml`)

In your k3s cluster:

- **Traefik** — ships with k3s by default
- **cert-manager** — `kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml`
- **Cloudflare API token secret** (DNS-01 TLS) — see step 1 below

### Step 1 — Cloudflare API token (DNS-01 only)

If you're using DNS-01 challenge (required for `.lan` / private hostnames):

```bash
# Create the secret in the cert-manager namespace
kubectl create secret generic cloudflare-api-token-secret \
  --from-literal=api-token=YOUR_CF_TOKEN \
  -n cert-manager
```

Token needs **Zone → DNS → Edit** permission on your domain. Create it at:  
Cloudflare Dashboard → My Profile → API Tokens → Create Token

### Step 2 — Edit `k8s/install-config.yaml`

```bash
nano k8s/install-config.yaml
```

Minimum required changes:

```yaml
patchpilot:
  network:
    hostname: patchpilot.yourdomain.com      # ← your real hostname
    additionalHostnames:
      - patchpilot.lan                        # ← optional internal hostname

  certManager:
    email: you@yourdomain.com                 # ← Let's Encrypt contact email
    cloudflare:
      email: you@cloudflare.com               # ← Cloudflare account email

  postgres:
    storageClass: "app-data"                  # ← your StorageClass, or "" for default

  storage:
    storageClass: "app-data"                  # ← same SC for backups/ansible
```

Everything else has safe defaults. Passwords and the Fernet encryption key are auto-generated if left blank.

### Step 3 — Run the installer

```bash
./install.sh --k3s
```

The installer will:
1. Verify Docker, kubectl, Python prerequisites
2. Build backend and frontend images locally
3. Log in to Docker Hub and push both images (`linit01/patchpilot-backend`, `linit01/patchpilot-frontend`)
4. Create a `patchpilot-dockerhub` imagePullSecret in the cluster so k3s can pull from the private repo
5. Generate rendered Kubernetes manifests in `k8s/.generated/`
6. Apply them to your cluster in dependency order
7. Wait for all deployments to roll out

**Dashboard:** `https://patchpilot.yourdomain.com`

### Useful k3s commands

```bash
# Watch pod status
kubectl get pods -n patchpilot -w

# Backend logs
kubectl logs -n patchpilot -l app=patchpilot-backend -f

# Frontend logs
kubectl logs -n patchpilot -l app=patchpilot-frontend -f

# TLS certificate status
kubectl describe cert patchpilot-tls -n patchpilot

# Get all PatchPilot resources
kubectl get all -n patchpilot

# Uninstall
./k8s/install-k3s.sh --uninstall
```

---

## First Steps After Install

### 1 — Add an SSH Key

1. Open **Settings → SSH Keys → Add SSH Key**
2. Name it (e.g. `homelab-key`)
3. Click **Choose Key File** and select `~/.ssh/id_ed25519` (or your key)
4. Check **Set as default key**
5. **Save Key**

### 2 — Add a Host

1. **Settings → Hosts → Add New Host**
2. Fill in:
   - **Hostname:** server IP or FQDN
   - **SSH User:** your username (default set in config)
   - **SSH Port:** 22
   - **Authentication:** select your saved key
3. Click **Test Connection** to verify
4. **Save Host**

PatchPilot runs a background check on the new host within 30 seconds.

### 3 — Review the Dashboard

After the first check completes you'll see:

- Stats cards: hosts up to date / need updates / unreachable / total pending packages
- Host table with status badges and last-checked timestamps
- **View Details** → per-host package list with current and available versions

### 4 — Patch

1. Select one or more hosts using the checkboxes
2. Click **Patch Selected**
3. Enter the sudo password
4. Watch the real-time progress stream
5. Dashboard auto-refreshes on completion

---

## Troubleshooting

**Hosts don't appear after adding**  
Wait 30–60 seconds for the background check. Or click **Refresh Status** to trigger immediately.

**SSH connection test fails**  
```bash
# Test from the backend container directly
docker exec -it patchpilot-backend-1 ssh -v -i /root/.ssh/id_rsa user@host
# k3s
kubectl exec -n patchpilot deploy/patchpilot-backend -- ssh -v user@host
```

**Patching fails with permission denied**  
The sudo password must match what's set on the target host for the SSH user. Test with `sudo -v` on the host first.

**TLS not issuing (k3s)**  
```bash
kubectl describe cert patchpilot-tls -n patchpilot
kubectl logs -n cert-manager deploy/cert-manager | tail -30
```
Most common cause: Cloudflare token is missing, wrong scope, or in the wrong namespace.

**Full documentation:** [README.md](README.md)
