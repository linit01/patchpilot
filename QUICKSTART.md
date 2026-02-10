# PatchPilot Quick Start Guide

Get PatchPilot running in **5 minutes** using Docker Compose.

## Prerequisites

- Docker and Docker Compose installed
- A free Supabase account ([sign up here](https://supabase.com))
- Your Ansible playbook and hosts file

## Step-by-Step Installation

### 1. Download PatchPilot

```bash
git clone https://github.com/yourusername/patchpilot.git
cd patchpilot
```

### 2. Run the Installer

```bash
./install.sh
```

The installer will:
- ✅ Check that Docker is installed
- ✅ Ask for your Supabase credentials
- ✅ Copy your Ansible files
- ✅ Build and start the services
- ✅ Open the dashboard in your browser

**That's it!** PatchPilot is now running.

## What You'll See

### On First Load

The dashboard will show "Loading hosts..." for about 30 seconds while PatchPilot runs its initial Ansible check of your systems.

### After Initial Check

You'll see:

1. **Statistics Cards** at the top:
   - Total hosts
   - Hosts up to date
   - Hosts needing updates
   - Unreachable hosts
   - Total pending updates

2. **Host Table** showing:
   - Each system's hostname
   - Current update status
   - Number of available updates
   - Last check time

3. **Action Buttons**:
   - "Refresh Status" - runs a new check
   - "Patch Selected" - updates chosen hosts

## Using PatchPilot

### Check for New Updates

Click **"Refresh Status"** to run an Ansible check across all your systems.

### Patch Your Systems

1. **Select hosts** using the checkboxes
2. Click **"Patch Selected"**
3. Enter your **sudo password**
4. Click **"Confirm Patch"**

PatchPilot will run the updates and automatically refresh the status when complete.

### View Package Details

Click **"View Details"** on any host to see:
- Which specific packages need updating
- Current and available versions
- Package names

## Common Tasks

### View Logs

```bash
docker-compose logs -f
```

### Stop PatchPilot

```bash
docker-compose down
```

### Restart Services

```bash
docker-compose restart
```

### Update PatchPilot

```bash
git pull
docker-compose up -d --build
```

## Troubleshooting

### Dashboard Shows No Hosts

- Wait 30-60 seconds after startup for the initial check
- Click "Refresh Status" manually
- Check logs: `docker-compose logs backend`

### Can't Connect to Hosts

Verify your SSH keys are accessible:

```bash
docker exec -it patchpilot-backend ls -la /root/.ssh
```

Test Ansible connectivity:

```bash
docker exec -it patchpilot-backend ansible all -i /ansible/hosts -m ping
```

### Patching Fails

- Verify you entered the correct sudo password
- Ensure hosts are reachable
- Check that your playbook works manually

### Backend Won't Start

Check Supabase credentials:

```bash
docker exec -it patchpilot-backend env | grep SUPABASE
```

## Next Steps

### Schedule Automatic Checks

Add a cron job to check for updates daily:

```bash
0 9 * * * cd /path/to/patchpilot && docker exec patchpilot-backend curl -X POST http://localhost:8000/api/check
```

### Deploy to Production

See [KUBERNETES.md](KUBERNETES.md) for deploying to your Kubernetes cluster.

### Customize

- Edit `ansible/check-os-updates.yml` to modify checks
- Adjust auto-refresh in `frontend/app.js`
- Add email notifications in `backend/app.py`

## Getting Help

- 📖 Full docs: [README.md](README.md)
- 🐛 Report issues: [GitHub Issues](https://github.com/yourusername/patchpilot/issues)
- 💬 Community: [Discord](https://discord.gg/patchpilot)

---

**You're all set!** Enjoy automated patch management with PatchPilot 🎯

### Step 1: Set up Supabase (2 minutes)

1. Go to https://supabase.com and sign up (free)
2. Create a new project
3. Go to SQL Editor and run the contents of `database-schema.sql`
4. Go to Settings > API and copy:
   - Project URL
   - anon/public key

### Step 2: Configure the Application (1 minute)

1. Copy your environment file:
   ```bash
   cd patch-dashboard
   cp .env.example .env
   ```

2. Edit `.env` with your Supabase credentials:
   ```
   SUPABASE_URL=https://xxxxx.supabase.co
   SUPABASE_KEY=your-key-here
   ```

3. Copy your Ansible files:
   ```bash
   mkdir -p ansible
   cp ~/check-os-updates.yml ansible/
   cp ~/hosts ansible/
   ```

### Step 3: Deploy (2 minutes)

```bash
./deploy.sh
# Select option 1 for Local Dev
```

That's it! The dashboard will open at http://localhost:8080

## What You'll See

1. **Stats Cards** showing:
   - Total hosts
   - Hosts up to date
   - Hosts needing updates
   - Unreachable hosts
   - Total pending updates

2. **Host Table** with:
   - Checkbox to select hosts
   - Hostname, IP, OS type
   - Status badge
   - Number of updates available
   - Last checked time
   - View Details button

3. **Action Buttons**:
   - Refresh Status (runs Ansible check)
   - Patch Selected (patches chosen hosts)

## Using the Dashboard

### Check for Updates
Click "Refresh Status" - this runs your Ansible playbook and updates the database.

### Patch Hosts
1. Select one or more hosts using checkboxes
2. Click "Patch Selected"
3. Enter your sudo password
4. Confirm

The system will:
- Run Ansible with the apply-updates tag
- Patch the selected hosts
- Update the dashboard after completion

### View Host Details
Click "View Details" on any host to see:
- Host information
- List of pending packages
- Current and available versions

## Next Steps

### Deploy to Your k3s Cluster

See the main README.md for Kubernetes deployment instructions.

### Customize

- Modify the Ansible playbook to add more checks
- Adjust the auto-refresh interval in `frontend/app.js`
- Add email notifications in `backend/app.py`
- Create a scheduled CronJob to run checks automatically

### Monitor

Watch the logs:
```bash
docker-compose logs -f backend
```

## Troubleshooting

**Dashboard shows no hosts:**
- Wait 10 seconds after startup for initial check
- Click "Refresh Status" manually
- Check backend logs for errors

**Ansible connection fails:**
- Verify SSH keys are accessible
- Test Ansible manually:
  ```bash
  docker exec -it patch-dashboard-backend-1 \
    ansible all -i /ansible/hosts -m ping
  ```

**Can't patch hosts:**
- Make sure you entered the correct sudo password
- Check that hosts are reachable
- Verify the Ansible playbook works manually

## Support

Check the main README.md for detailed documentation and troubleshooting.
