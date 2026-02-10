# PatchPilot 🎯

**Automated system update management for your infrastructure**

A beautiful, modern web dashboard for monitoring and managing OS patches across all your Linux and macOS systems. Built with FastAPI, Supabase, and powered by Ansible.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)
![Kubernetes](https://img.shields.io/badge/kubernetes-supported-blue.svg)

## ✨ Features

- 🎯 **Real-time Monitoring** - Track patch status across all your systems
- ⚡ **One-Click Patching** - Select and patch multiple hosts simultaneously  
- 📊 **Beautiful Dashboard** - Grafana-style interface with live statistics
- 🔄 **Automated Checks** - Scheduled update scanning via Ansible
- 📈 **History Tracking** - Full audit trail of all patch operations
- 🐧 **Multi-OS Support** - Works with Debian/Ubuntu Linux and macOS
- 🔒 **Secure** - SSH key authentication, encrypted credentials
- 📦 **Easy Deploy** - Up and running in 5 minutes with Docker Compose

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose installed
- A free Supabase account ([sign up here](https://supabase.com))
- Your existing Ansible playbook and inventory

### Installation (5 minutes)

```bash
# 1. Clone or download PatchPilot
git clone https://github.com/yourusername/patchpilot.git
cd patchpilot

# 2. Run the installer
./install.sh

# 3. Access the dashboard
open http://localhost:8080
```

That's it! PatchPilot will automatically:
- Set up the database schema in Supabase
- Configure your environment
- Copy your Ansible files
- Start the services
- Run an initial system check

## 📸 Screenshots

### Main Dashboard
View all your systems at a glance with real-time update status.

### Host Details  
Drill down into specific hosts to see exactly which packages need updating.

### One-Click Patching
Select multiple hosts and patch them all with a single click.

## 🎓 How It Works

1. **Ansible Integration**: PatchPilot runs your existing Ansible playbooks to check for updates
2. **Database Storage**: Results are stored in Supabase for historical tracking
3. **Web Dashboard**: Beautiful interface shows current status and allows patching
4. **Automated Updates**: Optionally schedule automatic checks and patches

## 📋 Deployment Options

### Docker Compose (Recommended)

Perfect for homelabs, small teams, or testing:

```bash
docker-compose up -d
```

**Pros:**
- ✅ Simple one-command deployment
- ✅ Works on any system with Docker
- ✅ Easy to update and maintain
- ✅ Perfect for 1-50 hosts

### Kubernetes

For production environments and large deployments:

```bash
kubectl apply -f k8s/
```

**Pros:**
- ✅ High availability
- ✅ Auto-scaling
- ✅ GitOps with ArgoCD
- ✅ Enterprise-ready

See [KUBERNETES.md](KUBERNETES.md) for detailed k8s deployment instructions.

## 📖 Documentation

- [Quick Start Guide](QUICKSTART.md) - Get up and running in 5 minutes
- [Docker Deployment](docs/DOCKER.md) - Detailed Docker Compose setup
- [Kubernetes Deployment](docs/KUBERNETES.md) - Full k8s configuration
- [Configuration Guide](docs/CONFIGURATION.md) - All configuration options
- [API Documentation](docs/API.md) - REST API reference
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues and solutions

## 🎯 Use Cases

### Homelab Enthusiasts
- Track updates across all your homelab systems
- One dashboard for Linux servers, NAS, and macOS machines
- Schedule overnight patching windows

### Small Businesses  
- Ensure all workstations and servers are up to date
- Compliance reporting for audits
- Reduce manual maintenance overhead

### DevOps Teams
- Centralized patch management for dev/staging environments  
- Integration with CI/CD pipelines
- Historical tracking for change management

## 🔧 Configuration

### Supported Operating Systems

**Linux:**
- ✅ Debian/Ubuntu (apt)
- ✅ RHEL/Rocky/CentOS (dnf) - Coming soon
- ✅ Arch (pacman) - Coming soon

**macOS:**
- ✅ Homebrew packages
- ✅ System updates via softwareupdate
- ✅ App Store updates via mas

### Environment Variables

```bash
# Required
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key

# Optional
ANSIBLE_PLAYBOOK_PATH=/ansible/check-os-updates.yml
ANSIBLE_INVENTORY_PATH=/ansible/hosts
AUTO_CHECK_INTERVAL=3600  # Seconds between checks
```

## 🛠️ Development

Want to contribute or customize PatchPilot?

```bash
# Backend development
cd backend
pip install -r requirements.txt
uvicorn app:app --reload

# Frontend development  
cd frontend
python -m http.server 8080
```

## 🗺️ Roadmap

- [ ] Email/Slack notifications
- [ ] Scheduled automatic patching
- [ ] Package-level selection
- [ ] Rollback capability
- [ ] Windows update support
- [ ] Prometheus metrics export
- [ ] Multi-tenancy for MSPs
- [ ] Mobile app

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Database powered by [Supabase](https://supabase.com)
- Automation via [Ansible](https://www.ansible.com/)
- Inspired by the DevOps and homelab communities

## 💬 Support

- 📧 Email: support@patchpilot.io
- 💬 Discord: [Join our community](https://discord.gg/patchpilot)
- 🐛 Issues: [GitHub Issues](https://github.com/yourusername/patchpilot/issues)

## ⭐ Star History

If you find PatchPilot useful, please consider giving it a star on GitHub!

---

**Made with ❤️ for the homelab and DevOps communities**

### 1. Set up Supabase

1. Create a new project at [supabase.com](https://supabase.com)
2. Run the SQL schema:
   ```bash
   # Copy the contents of database-schema.sql into Supabase SQL Editor
   ```
3. Get your project URL and anon key from Settings > API

### 2. Configure Environment

```bash
# Copy example env file
cp .env.example .env

# Edit .env with your Supabase credentials
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-anon-key
```

### 3. Set up Ansible Files

```bash
# Create ansible directory
mkdir -p ansible

# Copy your Ansible playbook and inventory
cp ~/check-os-updates.yml ansible/
cp ~/hosts ansible/
```

### 4. Run with Docker Compose

```bash
# Build and start services
docker-compose up -d

# View logs
docker-compose logs -f

# Access the dashboard
open http://localhost:8080
```

The dashboard will:
- Run an initial Ansible check on startup
- Display all hosts and their update status
- Allow you to select hosts and trigger patches

## Kubernetes Deployment

### 1. Prepare Secrets

```bash
# Create SSH key secret
kubectl create secret generic ssh-keys \
  --from-file=id_rsa=~/.ssh/your_key \
  --from-file=id_rsa.pub=~/.ssh/your_key.pub \
  -n patch-dashboard

# Update k8s/deployment.yaml with your:
# - Supabase URL and key
# - Domain name
# - Docker image registry
```

### 2. Create ConfigMaps

```bash
# Create ansible config configmap
kubectl create configmap ansible-config \
  --from-file=check-os-updates.yml=ansible/check-os-updates.yml \
  --from-file=hosts=ansible/hosts \
  -n patch-dashboard

# Create frontend files configmap
kubectl create configmap frontend-files \
  --from-file=frontend/ \
  -n patch-dashboard

# Create nginx config configmap
kubectl create configmap nginx-config \
  --from-file=nginx.conf=nginx.conf \
  -n patch-dashboard
```

### 3. Deploy

```bash
# Apply the deployment
kubectl apply -f k8s/deployment.yaml

# Check status
kubectl get pods -n patch-dashboard

# Access via ingress
# https://patch.yourdomain.com
```

### 4. ArgoCD Setup (Optional)

If you're using ArgoCD:

```yaml
# Create Application manifest
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: patch-dashboard
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/yourusername/patch-dashboard
    targetRevision: main
    path: k8s
  destination:
    server: https://kubernetes.default.svc
    namespace: patch-dashboard
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

## API Endpoints

### Get All Hosts
```bash
GET /api/hosts
```

### Get Host Details
```bash
GET /api/hosts/{hostname}
```

### Get Host Packages
```bash
GET /api/hosts/{hostname}/packages
```

### Trigger Check
```bash
POST /api/check
```

### Trigger Patch
```bash
POST /api/patch
{
  "hostnames": ["host1", "host2"],
  "become_password": "your-sudo-password"
}
```

### Get Statistics
```bash
GET /api/stats
```

### Get Patch History
```bash
GET /api/history?hostname=host1&limit=50
```

## Usage

### Dashboard Features

1. **Overview Cards**: Shows total hosts, update status, and pending updates
2. **Host Table**: Lists all hosts with their current status
3. **Select Hosts**: Use checkboxes to select hosts for patching
4. **Refresh**: Manually trigger an Ansible check
5. **Patch Selected**: Apply updates to selected hosts
6. **View Details**: Click on any host to see pending package details

### Automated Checks

The backend runs an Ansible check:
- On startup
- When you click "Refresh Status"
- Optionally, schedule a cron job or k8s CronJob for periodic checks

### Patching Workflow

1. Select one or more hosts using checkboxes
2. Click "Patch Selected"
3. Enter sudo password
4. Confirm operation
5. Monitor status in the dashboard
6. Dashboard auto-refreshes after patch completion

## Customization

### Add More OS Types

Edit `ansible_runner.py` to parse additional OS update formats:
- RedHat/Rocky: `dnf check-update`
- Arch: `pacman -Qu`
- Alpine: `apk list -u`

### Change Update Frequency

Modify the auto-refresh interval in `frontend/app.js`:
```javascript
// Default: 5 minutes
setInterval(loadDashboard, 5 * 60 * 1000);
```

### Add Monitoring

Integrate with Prometheus/Grafana:
- Add metrics endpoint in FastAPI
- Expose patch status as metrics
- Create Grafana dashboards

## Troubleshooting

### Ansible Fails to Connect

```bash
# Check SSH keys are mounted correctly
docker exec -it patch-dashboard-backend ls -la /root/.ssh

# Test Ansible manually
docker exec -it patch-dashboard-backend ansible all -i /ansible/hosts -m ping
```

### Database Connection Issues

```bash
# Verify Supabase credentials
docker exec -it patch-dashboard-backend env | grep SUPABASE

# Check database logs in Supabase dashboard
```

### Frontend Not Loading

```bash
# Check nginx logs
docker logs patch-dashboard-frontend

# Verify API is accessible
curl http://localhost:8000/api/stats
```

## Development

### Backend Development

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --reload
```

### Frontend Development

Simply edit HTML/CSS/JS files and refresh your browser.
Or use a local server:

```bash
cd frontend
python -m http.server 8080
```

## Security Considerations

1. **SSH Keys**: Ensure proper permissions (600) on private keys
2. **Sudo Password**: Transmitted via API - use HTTPS in production
3. **Supabase RLS**: Enable Row Level Security policies for multi-user scenarios
4. **Network**: Restrict access to dashboard via firewall/ingress rules
5. **Secrets**: Use Kubernetes secrets or HashiCorp Vault for production

## Future Enhancements

- [ ] Email/Slack notifications for patch completion
- [ ] Scheduled automatic patching windows
- [ ] Package-level selection (not just host-level)
- [ ] Rollback capability
- [ ] Compliance reporting
- [ ] Integration with monitoring tools
- [ ] Support for Windows updates
- [ ] Multi-tenancy support

## Contributing

This is a personal project but feel free to fork and customize for your needs!

## License

MIT License - See LICENSE file for details

## Author

Built by John for homelab patch management automation.
