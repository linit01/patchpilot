# PatchPilot — Kubernetes Deployment Guide (K3s + Traefik + cert-manager)

> This guide is written for a K3s cluster that uses **Traefik** as the ingress
> controller and **cert-manager** for automated TLS certificates.  Your
> `example.com` external domain goes through Cloudflare, and your `.lan`
> internal domain is served by PiHole — both are handled by a single
> **DNS-01 Cloudflare challenge** so no public HTTP port is required.

---

## Architecture

```
Browser (HTTPS)
    │
    ▼
Cloudflare CDN / DNS  ──────────────────────────────────────────────────────►
    │                                                            example.com
    ▼
Traefik (K3s ingress)   ← terminates TLS using cert issued by cert-manager
    │    ↑ reads TLS secret from cert-manager
    │
    ├─► patchpilot-frontend (nginx:alpine)  — serves SPA
    │       │
    │       └─► patchpilot-backend (FastAPI)  — API + WebSocket
    │               │
    │               └─► postgres (PostgreSQL 15)
    │
PiHole  →  patchpilot.lan  →  same Traefik LoadBalancer IP
```

---

## Prerequisites

| Component | Install command / URL |
|-----------|----------------------|
| K3s | `curl -sfL https://get.k3s.io | sh -` |
| cert-manager | `kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml` |
| Cloudflare API token | Dashboard → My Profile → API Tokens → Create Token → Zone:DNS:Edit |

---

## Step 1 — Cloudflare API token Secret

cert-manager needs this to create DNS TXT records for the ACME challenge.

```bash
kubectl create secret generic cloudflare-api-token-secret \
  -n cert-manager \
  --from-literal=api-token=YOUR_CLOUDFLARE_API_TOKEN
```

The token needs **Zone → DNS → Edit** permission scoped to `example.com`.

---

## Step 2 — Edit `k8s/deployment.yaml`

Open the file and change the following placeholders:

| Placeholder | Replace with |
|---|---|
| `your-email@example.com` | Your email for Let's Encrypt notifications |
| `your-cloudflare-email@example.com` | Email tied to your Cloudflare account |
| `CHANGE_ME_STRONG_PASSWORD` | Strong PostgreSQL password |
| `CHANGE_ME_FERNET_KEY` | Output of `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `your-registry/patchpilot-backend:latest` | Your container image reference |
| `patchpilot.example.com` | Your actual external hostname |
| `patchpilot.lan` | Your actual internal `.lan` hostname |

---

## Step 3 — Build and push the backend image

```bash
cd /path/to/patchpilot
docker build -t your-registry/patchpilot-backend:latest .
docker push your-registry/patchpilot-backend:latest
```

For a local K3s node you can also import the image directly:

```bash
docker save your-registry/patchpilot-backend:latest | k3s ctr images import -
```

---

## Step 4 — Apply manifests

```bash
kubectl apply -f k8s/deployment.yaml

# Watch cert-manager issue the certificate (usually 30-60 s with DNS-01)
kubectl get certificate -n patchpilot -w

# Watch pods come up
kubectl get pods -n patchpilot -w
```

---

## Step 5 — DNS

### External (Cloudflare)
Add an **A record** in Cloudflare:
```
Type: A
Name: patchpilot
Value: <your K3s node / LoadBalancer IP>
Proxy: DNS only (gray cloud) initially to test, then orange cloud
```

### Internal (.lan via PiHole)
In PiHole → Local DNS → DNS Records add:
```
patchpilot.lan → <your K3s node / LoadBalancer IP>
```

---

## Step 6 — Verify HTTPS

```bash
curl -I https://patchpilot.example.com
# Expect: HTTP/2 200 and Strict-Transport-Security header from Traefik
```

---

## Environment Variables (backend)

| Variable | Default | Description |
|---|---|---|
| `APP_BASE_URL` | `http://localhost:8080` | Public URL of PatchPilot (set in Secret) |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins — set explicitly in production |
| `PATCHPILOT_ENCRYPTION_KEY` | — | Fernet key for credential encryption |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `BACKUP_DIR` | `/backups` | Backup storage path |

---

## Configuring via the UI (General Settings → Network & Security)

After deployment browse to `https://patchpilot.example.com/settings.html`
and open the **General** tab.  Scroll to **Network & Security**:

- **Application Base URL** — paste `https://patchpilot.example.com`
- **Allowed Origins** — paste `https://patchpilot.example.com,https://patchpilot.lan`

Click **Save Network Settings**.  These values are stored in the database as
a reference for other users and for display.  The live CORS enforcement is
controlled by the `ALLOWED_ORIGINS` environment variable — update the Secret
and restart the backend pod to apply origin changes.

---

## Troubleshooting

```bash
# Certificate not issued?
kubectl describe certificate patchpilot-tls -n patchpilot
kubectl describe certificaterequest -n patchpilot
kubectl logs -n cert-manager deploy/cert-manager

# Pod not starting?
kubectl describe pod -n patchpilot -l app=patchpilot-backend
kubectl logs -n patchpilot deploy/patchpilot-backend

# CORS errors in browser?
# 1. Check ALLOWED_ORIGINS in the Secret matches the URL in the browser address bar exactly
# 2. Restart backend pod:  kubectl rollout restart deploy/patchpilot-backend -n patchpilot
```
