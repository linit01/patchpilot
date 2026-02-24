# PatchPilot — Kubernetes / K3s Deployment Guide

This guide covers deploying PatchPilot on a **K3s cluster** using:

- **Traefik** — ingress controller (ships with k3s by default)
- **cert-manager** — automated TLS certificate provisioning
- **Let's Encrypt** — free TLS certificates via DNS-01 (Cloudflare) or HTTP-01
- **PostgreSQL 15** — in-cluster database with persistent storage

> Your setup: Traefik terminates TLS, Cloudflare DNS handles `example.com`,
> PiHole resolves `.lan` hostnames internally — all covered by a single
> DNS-01 challenge (no public port 80 required for certificate issuance).

---

## Architecture

```
Browser (HTTPS)
     │
     ▼
Cloudflare DNS / CDN  ─────────────────────────────── example.com
     │
     ▼
Traefik (k3s LoadBalancer)
 • Terminates TLS (cert from cert-manager)
 • Routes /api/* and /ws/* to backend service
 • Routes / to frontend service
 • Enforces HTTP→HTTPS redirect + HSTS
     │
     ├──► patchpilot-frontend (Nginx / static SPA)
     │         │
     │    /api/* and /ws/*
     │         ▼
     └──► patchpilot-backend (FastAPI + Ansible)
               │
               ▼
          PostgreSQL 15
          (PVC — app-data StorageClass)

PiHole → patchpilot.lan → same Traefik LoadBalancer IP
```

---

## Prerequisites

### On the cluster

| Component | Status | Install |
|-----------|--------|---------|
| K3s | Required | `curl -sfL https://get.k3s.io \| sh -` |
| Traefik | Ships with k3s | Auto-installed |
| cert-manager | Required | See below |

```bash
# Install cert-manager (if not already present)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# Verify it's ready (wait ~60s)
kubectl get pods -n cert-manager
```

### On your build machine (Mac or Linux workstation)

| Requirement | Notes |
|-------------|-------|
| **Docker** (Desktop or Engine) | ⚠️ Must be **running** — used to build and push images from here |
| `kubectl` | Configured with kubeconfig pointing at your k3s cluster |
| `python3` + PyYAML | `pip3 install pyyaml` |
| Docker Hub access | `linit01/patchpilot` private repo — needs username + access token |

> **How images get into k3s:** The installer builds images with Docker on your machine,
> pushes them to Docker Hub as `linit01/patchpilot:backend-<tag>` and `linit01/patchpilot:frontend-<tag>`
> (single repo, component encoded in the tag), and creates a Kubernetes `imagePullSecret` so k3s can pull from the private repo.
> This works identically from macOS, Linux, or Windows — no SSH to k3s nodes required.
>
> Use a Docker Hub **Access Token** (not your account password):
> hub.docker.com → Account Settings → Security → New Access Token

---

## Step 1 — Cloudflare API Token Secret

cert-manager needs a Cloudflare API token to create DNS TXT records for the ACME DNS-01 challenge. This is what lets you get certificates for `.lan` hostnames that can't do HTTP-01.

**Create the token in Cloudflare:**
1. Dashboard → My Profile → API Tokens → Create Token
2. Use the "Edit zone DNS" template
3. Scope it to **your zone only** (`example.com`) — least-privilege
4. Copy the token

**Create the Kubernetes secret:**
```bash
kubectl create secret generic cloudflare-api-token-secret \
  --from-literal=api-token=YOUR_CLOUDFLARE_API_TOKEN \
  -n cert-manager
```

> The secret must be in the `cert-manager` namespace — not in `patchpilot`.

---

## Step 2 — Configure `k8s/install-config.yaml`

This is the single source of truth for the entire k3s deployment. Open it and set your values:

```yaml
patchpilot:
  namespace: patchpilot

  network:
    hostname: patchpilot.example.com      # Primary external hostname
    additionalHostnames:
      - patchpilot.lan                         # Internal PiHole-resolved hostname

    tls:
      enabled: true
      clusterIssuer: letsencrypt-prod          # Must match ClusterIssuer name below

    httpsRedirect: true
    securityHeaders: true
    ingressClass: traefik

  certManager:
    createClusterIssuer: true
    email: you@example.com                 # Let's Encrypt registration email
    challengeType: dns01-cloudflare

    cloudflare:
      email: you@cloudflare.com                # Cloudflare account email
      apiTokenSecretName: cloudflare-api-token-secret

  postgres:
    user: patchpilot
    password: ""                               # Auto-generated if blank
    database: patchpilot
    storageSize: 5Gi
    storageClass: "app-data"                   # Your StorageClass name

  app:
    encryptionKey: ""                          # Auto-generated Fernet key (save the output!)
    autoRefreshInterval: 300
    defaultSshUser: root
    defaultSshPort: 22
    backupRetainCount: 3
    maxBackupSizeMb: 500

  storage:
    backupsSize: 10Gi
    ansibleSize: 1Gi
    storageClass: "app-data"

  ansible:
    playbookPath: "/path/to/check-os-updates.yml"   # Optional — seeds the PVC at deploy
    inventoryPath: "/path/to/hosts"                  # Optional — seeds the PVC at deploy
```

**Key decisions:**

| Setting | Choice | Why |
|---------|--------|-----|
| `challengeType` | `dns01-cloudflare` | Required for `.lan` hostname (no public HTTP) |
| `storageClass` | `app-data` | Your TrueNAS democratic-csi or equivalent SC |
| `encryptionKey` | blank = auto-generate | On first install let it generate; on upgrade paste the existing key to preserve encrypted SSH secrets |

---

## Step 3 — Run the Installer

```bash
./install.sh --k3s

# or directly
./k8s/install-k3s.sh
```

**Flags:**

| Flag | Effect |
|------|--------|
| `--dry-run` | Generate manifests in `k8s/.generated/` without applying |
| `--interactive` | Force prompts for every setting |
| `--uninstall` | Delete the `patchpilot` namespace and ClusterIssuer |
| `--config path/to/file.yaml` | Use an alternate config file |

**What the installer does:**

1. Checks prerequisites (Docker running, kubectl reachable, config present)
2. Reads `install-config.yaml` — auto-generates any blank passwords/keys
3. Builds `linit01/patchpilot:backend-0.9.4-alpha` and `linit01/patchpilot:frontend-0.9.4-alpha` with Docker
4. Imports images into k3s:
   - **macOS / remote host:** SSHes to the k3s node and pipes `sudo k3s ctr images import`
   - **Linux local k3s node:** `sudo k3s ctr images import` directly
5. Renders all manifest templates into `k8s/.generated/` (using `envsubst`)
7. Applies them in order: namespace → secrets → PVCs → PostgreSQL → backend → frontend → middlewares → certificate → ingress → ClusterIssuer
8. Waits for rollout completion
9. Prints the dashboard URL and useful debug commands

---

## Step 4 — Verify Deployment

```bash
# All pods running?
kubectl get pods -n patchpilot

# Expected output:
# NAME                                    READY   STATUS    RESTARTS
# patchpilot-postgres-xxxxx               1/1     Running   0
# patchpilot-backend-xxxxx                1/1     Running   0
# patchpilot-frontend-xxxxx               1/1     Running   0

# Certificate issued? (may take 1–3 minutes)
kubectl describe cert patchpilot-tls -n patchpilot
# Look for: Status: True, Reason: Ready

# Ingress configured?
kubectl get ingress -n patchpilot

# Services?
kubectl get svc -n patchpilot
```

---

## Step 5 — DNS

**External (Cloudflare):**
- `patchpilot.example.com` → your k3s LoadBalancer IP or node IP
- Set proxy status to **DNS only** (grey cloud) initially until you confirm it works

**Internal (PiHole):**
- `patchpilot.lan` → same k3s LoadBalancer IP
- Add as a Local DNS record in PiHole admin

To find the LoadBalancer/ingress IP:
```bash
kubectl get svc -n kube-system | grep traefik
# or
kubectl get nodes -o wide
```

---

## Persistent Storage

PatchPilot creates three PVCs in the `patchpilot` namespace:

| PVC | Default Size | Purpose |
|-----|-------------|---------|
| `postgres-data` | 5 Gi | PostgreSQL data |
| `patchpilot-backups` | 10 Gi | Backup archives |
| `ansible-data` | 1 Gi | Ansible playbook + inventory |

All use the `storageClass` defined in `install-config.yaml`. Leave blank to use the cluster default (k3s ships with `local-path`).

> **TrueNAS + democratic-csi:** Use your iSCSI or NFS storage class name (e.g. `truenas-iscsi`).

---

## Seeding Ansible Files

If you set `playbookPath` and `inventoryPath` in `install-config.yaml`, the installer creates a ConfigMap and a one-shot Job that copies those files into the `ansible-data` PVC on first deploy.

To verify:
```bash
kubectl wait --for=condition=complete job/patchpilot-ansible-init -n patchpilot --timeout=60s
kubectl exec -n patchpilot deploy/patchpilot-backend -- ls -la /ansible/
```

To copy files manually instead:
```bash
kubectl cp ./ansible/check-os-updates.yml patchpilot/<backend-pod-name>:/ansible/check-os-updates.yml
kubectl cp ./ansible/hosts patchpilot/<backend-pod-name>:/ansible/hosts
```

---

## Upgrading

```bash
# Edit install-config.yaml if needed (e.g. new hostname, changed storage class)
# ⚠️  Paste your existing encryptionKey if you have one — changing it orphans encrypted secrets

git pull   # or update your local checkout

./k8s/install-k3s.sh
# The installer rebuilds images, re-imports, and re-applies all manifests
# kubectl applies are idempotent — only changed resources are updated
```

---

## SSH Keys for Managed Hosts

The backend pod needs to SSH into your managed hosts. Add SSH keys via the UI (Settings → SSH Keys), or mount a pre-existing key:

```bash
# Create a secret from an existing key file
kubectl create secret generic patchpilot-ssh-keys \
  -n patchpilot \
  --from-file=id_rsa=/path/to/your/private_key

# The backend mounts this at /root/.ssh/ (mode 0600, optional — won't block startup if missing)
```

---

## Troubleshooting

### Certificate not issuing

```bash
kubectl describe cert patchpilot-tls -n patchpilot
kubectl describe certificaterequest -n patchpilot
kubectl logs -n cert-manager deploy/cert-manager | grep -i error
```

Common causes:
- Cloudflare token secret is in the wrong namespace (must be `cert-manager`, not `patchpilot`)
- Token scope is wrong (needs Zone:DNS:Edit)
- DNS propagation not complete yet (wait up to 5 min)

### Image push fails / k3s can't pull image

```bash
# Test Docker Hub login manually
docker login --username linit01

# Verify the image exists on Hub
docker pull linit01/patchpilot:backend-0.9.4-alpha
docker pull linit01/patchpilot:frontend-0.9.4-alpha

# Check the imagePullSecret is correct in the cluster
kubectl get secret dockerhub-pull-secret -n patchpilot -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d

# Recreate the pull secret manually if needed
kubectl create secret docker-registry dockerhub-pull-secret \
  --namespace=patchpilot \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=linit01 \
  --docker-password=YOUR_ACCESS_TOKEN \
  --dry-run=client -o yaml | kubectl apply -f -

# Check k3s can see the image after a pull
kubectl run test-pull --image=linit01/patchpilot:backend-0.9.4-alpha \
  --image-pull-policy=Always --restart=Never -n patchpilot
kubectl delete pod test-pull -n patchpilot
```

### Backend crashlooping

```bash
kubectl logs -n patchpilot -l app=patchpilot-backend --previous
kubectl describe pod -n patchpilot -l app=patchpilot-backend
```

Most common: PostgreSQL not ready yet (the `wait-for-postgres` initContainer should handle this, but if the DB password in the Secret is wrong it will loop).

### Postgres won't start

```bash
kubectl logs -n patchpilot -l app=patchpilot-postgres
kubectl describe pvc postgres-data -n patchpilot
```

Check that your StorageClass exists and can provision a PVC:
```bash
kubectl get sc
kubectl get pvc -n patchpilot
```

### Reset everything (⚠️ data loss)

```bash
./k8s/install-k3s.sh --uninstall
# Confirm by typing the namespace name
./k8s/install-k3s.sh
```

---

## Generated Manifests

All rendered manifests land in `k8s/.generated/` after each installer run:

```
k8s/.generated/
├── 00-namespace.yaml
├── 01-secrets.yaml          ← contains auto-generated passwords — keep secure
├── 02-pvcs.yaml
├── 03-postgres.yaml
├── 04-backend.yaml
├── 05-frontend.yaml
├── 06-middlewares.yaml      ← HTTPS mode only
├── 07-certificate.yaml      ← HTTPS mode only
├── 08-ingress.yaml
├── 09-clusterissuer.yaml    ← if createClusterIssuer: true
└── 10-ansible-configmap.yaml  ← if ansible paths are set
```

These are plain Kubernetes YAML — check them into GitOps (ArgoCD / Flux) if you want declarative cluster state. Treat `01-secrets.yaml` like `.env` — don't commit it to a public repo.

For ArgoCD: apply the secrets separately (or use External Secrets Operator / Vault) and point ArgoCD at the remaining manifests.
