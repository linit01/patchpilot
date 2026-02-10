# PatchPilot - Kubernetes Deployment Guide

Deploy PatchPilot to your Kubernetes cluster for production use.

## Why Kubernetes?

Use K8s deployment when you need:
- High availability and auto-healing
- Scalability beyond single-host deployment
- GitOps with ArgoCD or Flux
- Integration with existing k8s infrastructure
- Enterprise-grade security and networking

## Prerequisites

- Kubernetes cluster (k3s, k8s, EKS, GKE, AKE, etc.)
- kubectl configured
- Helm (optional, recommended)
- cert-manager (for TLS)
- Ingress controller (nginx, traefik, etc.)

## Quick Deploy

### 1. Create Namespace

```bash
kubectl create namespace patchpilot
```

### 2. Create Secrets

**Supabase Credentials:**

```bash
kubectl create secret generic patchpilot-secrets \
  --from-literal=supabase-url='https://your-project.supabase.co' \
  --from-literal=supabase-key='your-anon-key' \
  -n patchpilot
```

**SSH Keys:**

```bash
kubectl create secret generic ssh-keys \
  --from-file=id_rsa=~/.ssh/your_key \
  --from-file=id_rsa.pub=~/.ssh/your_key.pub \
  --from-file=known_hosts=~/.ssh/known_hosts \
  -n patchpilot
```

**Become Password (optional):**

```bash
kubectl create secret generic ansible-become \
  --from-literal=password='your-sudo-password' \
  -n patchpilot
```

### 3. Create ConfigMaps

**Ansible Configuration:**

```bash
kubectl create configmap ansible-config \
  --from-file=check-os-updates.yml=ansible/check-os-updates.yml \
  --from-file=hosts=ansible/hosts \
  -n patchpilot
```

**Frontend Files:**

```bash
kubectl create configmap frontend-files \
  --from-file=frontend/ \
  -n patchpilot
```

**Nginx Config:**

```bash
kubectl create configmap nginx-config \
  --from-file=nginx.conf \
  -n patchpilot
```

### 4. Deploy Application

```bash
kubectl apply -f k8s/deployment.yaml
```

### 5. Verify Deployment

```bash
# Check pods
kubectl get pods -n patchpilot

# Check services
kubectl get svc -n patchpilot

# Check ingress
kubectl get ingress -n patchpilot

# View logs
kubectl logs -f deployment/patchpilot-backend -n patchpilot
```

## ArgoCD Deployment

### Option 1: Direct Application

```yaml
# argocd-application.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: patchpilot
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/yourusername/patchpilot.git
    targetRevision: main
    path: k8s
  destination:
    server: https://kubernetes.default.svc
    namespace: patchpilot
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

Apply:

```bash
kubectl apply -f argocd-application.yaml
```

### Option 2: With Kustomize

Create `kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: patchpilot

resources:
  - deployment.yaml

secretGenerator:
  - name: patchpilot-secrets
    literals:
      - supabase-url=https://your-project.supabase.co
      - supabase-key=your-key-here

configMapGenerator:
  - name: ansible-config
    files:
      - check-os-updates.yml=ansible/check-os-updates.yml
      - hosts=ansible/hosts
```

Then reference in ArgoCD application.

## Helm Deployment (Coming Soon)

We're working on an official Helm chart:

```bash
helm repo add patchpilot https://charts.patchpilot.io
helm install patchpilot patchpilot/patchpilot \
  --namespace patchpilot \
  --create-namespace \
  --set supabase.url=https://your-project.supabase.co \
  --set supabase.key=your-key
```

## Configuration Options

### Environment Variables

The backend deployment supports these environment variables:

```yaml
env:
  - name: SUPABASE_URL
    valueFrom:
      secretKeyRef:
        name: patchpilot-secrets
        key: supabase-url
  - name: SUPABASE_KEY
    valueFrom:
      secretKeyRef:
        name: patchpilot-secrets
        key: supabase-key
  - name: AUTO_CHECK_INTERVAL
    value: "3600"  # Check every hour
  - name: LOG_LEVEL
    value: "INFO"
```

### Resource Limits

Adjust based on your host count:

**Small deployment (1-50 hosts):**
```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

**Medium deployment (50-200 hosts):**
```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "250m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

**Large deployment (200+ hosts):**
```yaml
resources:
  requests:
    memory: "1Gi"
    cpu: "500m"
  limits:
    memory: "2Gi"
    cpu: "2000m"
```

### Persistence

For persistent data (logs, temporary files):

```yaml
volumeMounts:
  - name: data
    mountPath: /data
volumes:
  - name: data
    persistentVolumeClaim:
      claimName: patchpilot-data
```

## Ingress Configuration

### Nginx Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: patchpilot
  namespace: patchpilot
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - patchpilot.yourdomain.com
      secretName: patchpilot-tls
  rules:
    - host: patchpilot.yourdomain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: patchpilot-frontend
                port:
                  number: 80
```

### Traefik Ingress

```yaml
apiVersion: traefik.containo.us/v1alpha1
kind: IngressRoute
metadata:
  name: patchpilot
  namespace: patchpilot
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`patchpilot.yourdomain.com`)
      kind: Rule
      services:
        - name: patchpilot-frontend
          port: 80
  tls:
    certResolver: letsencrypt
```

## Monitoring & Observability

### Prometheus Metrics

Add ServiceMonitor for Prometheus Operator:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: patchpilot
  namespace: patchpilot
spec:
  selector:
    matchLabels:
      app: patchpilot-backend
  endpoints:
    - port: http
      path: /metrics
```

### Grafana Dashboard

Import the PatchPilot dashboard (dashboard ID coming soon).

### Loki Logging

Labels are automatically added for filtering:

```
{namespace="patchpilot", app="patchpilot-backend"}
```

## Security Best Practices

### Network Policies

Restrict traffic between pods:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: patchpilot-policy
  namespace: patchpilot
spec:
  podSelector:
    matchLabels:
      app: patchpilot-backend
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: patchpilot-frontend
  egress:
    - to:
        - podSelector: {}
      ports:
        - port: 443
          protocol: TCP
```

### Pod Security

Enable Pod Security Standards:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: patchpilot
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

### RBAC

Create minimal service account:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: patchpilot
  namespace: patchpilot
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: patchpilot
  namespace: patchpilot
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: patchpilot
  namespace: patchpilot
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: patchpilot
subjects:
  - kind: ServiceAccount
    name: patchpilot
```

## Backup & Disaster Recovery

### Database Backup

Supabase handles database backups automatically. For additional safety:

```bash
# Backup to S3/B2/etc
kubectl create cronjob patchpilot-backup \
  --image=supabase/postgres:15 \
  --schedule="0 2 * * *" \
  -- pg_dump ...
```

### Configuration Backup

```bash
# Backup secrets and configs
kubectl get secret,configmap -n patchpilot -o yaml > backup.yaml
```

## Scaling

### Horizontal Pod Autoscaler

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: patchpilot-backend
  namespace: patchpilot
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: patchpilot-backend
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

## Troubleshooting

### Check Pod Status

```bash
kubectl get pods -n patchpilot
kubectl describe pod <pod-name> -n patchpilot
```

### View Logs

```bash
kubectl logs -f deployment/patchpilot-backend -n patchpilot
kubectl logs -f deployment/patchpilot-frontend -n patchpilot
```

### Test Connectivity

```bash
# Test backend
kubectl port-forward svc/patchpilot-backend 8000:8000 -n patchpilot
curl http://localhost:8000/

# Test Ansible
kubectl exec -it deployment/patchpilot-backend -n patchpilot -- \
  ansible all -i /ansible/hosts -m ping
```

### Common Issues

**Pods in CrashLoopBackOff:**
- Check secrets are created correctly
- Verify Ansible files exist in configmap
- Check resource limits

**Can't access via Ingress:**
- Verify ingress controller is running
- Check cert-manager issued certificate
- Confirm DNS points to cluster

**Ansible fails to connect:**
- Verify SSH keys secret is mounted
- Check network policies allow egress
- Test from pod directly

## Upgrading

### Rolling Update

```bash
# Update image
kubectl set image deployment/patchpilot-backend \
  backend=your-registry/patchpilot:v1.1.0 \
  -n patchpilot

# Watch rollout
kubectl rollout status deployment/patchpilot-backend -n patchpilot
```

### With ArgoCD

Simply push to git and ArgoCD will sync automatically.

## Uninstall

```bash
# Delete namespace (removes everything)
kubectl delete namespace patchpilot

# Or individual resources
kubectl delete -f k8s/deployment.yaml
```

---

**Need help?** Join our [Discord](https://discord.gg/patchpilot) or open an [issue](https://github.com/yourusername/patchpilot/issues).
