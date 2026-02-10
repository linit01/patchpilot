"""
FastAPI application for patch management dashboard
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import os

from database import DatabaseClient
from ansible_runner import AnsibleRunner

app = FastAPI(title="PatchPilot API", version="1.0.0")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize clients
db = DatabaseClient()
ansible = AnsibleRunner(
    playbook_path=os.getenv("ANSIBLE_PLAYBOOK_PATH", "/ansible/check-os-updates.yml"),
    inventory_path=os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
)

# Pydantic models
class PatchRequest(BaseModel):
    hostnames: List[str]
    become_password: Optional[str] = None

class HostResponse(BaseModel):
    id: str
    hostname: str
    ip_address: Optional[str]
    os_type: Optional[str]
    os_family: Optional[str]
    status: str
    total_updates: int
    last_checked: Optional[datetime]

class PackageResponse(BaseModel):
    id: str
    host_id: str
    package_name: str
    current_version: Optional[str]
    available_version: Optional[str]
    update_type: Optional[str]

# Background task to run ansible check
def run_ansible_check_task():
    """Background task to run Ansible check and update database"""
    print(f"[{datetime.now()}] Running Ansible check...")
    
    success, hosts_data = ansible.run_check()
    
    if not success:
        print(f"Ansible check failed: {hosts_data.get('error', 'Unknown error')}")
        return
    
    # Update database with results
    for hostname, data in hosts_data.items():
        try:
            # Upsert host
            host = db.upsert_host(
                hostname=hostname,
                ip_address=data.get("ip_address", ""),
                os_type=data.get("os_type", ""),
                os_family=data.get("os_family", ""),
                status=data.get("status", "unknown"),
                total_updates=data.get("total_updates", 0)
            )
            
            print(f"Updated host: {hostname} - Status: {data.get('status')} - Updates: {data.get('total_updates')}")
            
        except Exception as e:
            print(f"Error updating host {hostname}: {e}")
    
    print(f"[{datetime.now()}] Ansible check completed")

# Background task to run ansible patch
def run_ansible_patch_task(hostnames: List[str], become_password: Optional[str] = None):
    """Background task to run Ansible patch on specified hosts"""
    print(f"[{datetime.now()}] Running Ansible patch on: {', '.join(hostnames)}")
    
    start_time = datetime.now()
    success, results = ansible.run_patch(limit_hosts=hostnames, become_password=become_password)
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds())
    
    # Record patch history for each host
    for hostname in hostnames:
        host = db.get_host_by_hostname(hostname)
        if host:
            db.add_patch_history(
                host_id=host["id"],
                packages_updated=[],  # Will be enhanced later
                success=success,
                error_message=None if success else results.get("error"),
                duration_seconds=duration
            )
    
    # Run a check after patching to update status
    if success:
        run_ansible_check_task()
    
    print(f"[{datetime.now()}] Ansible patch completed - Success: {success}")

# API Routes

@app.get("/")
def read_root():
    """Health check endpoint"""
    return {"status": "healthy", "service": "patchpilot"}

@app.get("/api/hosts", response_model=List[dict])
def get_hosts():
    """Get all hosts with their current status"""
    try:
        hosts = db.get_all_hosts()
        return hosts
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/hosts/{hostname}")
def get_host(hostname: str):
    """Get specific host details"""
    try:
        host = db.get_host_by_hostname(hostname)
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")
        return host
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/hosts/{hostname}/packages")
def get_host_packages(hostname: str):
    """Get all pending packages for a specific host"""
    try:
        host = db.get_host_by_hostname(hostname)
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")
        
        packages = db.get_packages_for_host(host["id"])
        return packages
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/check")
def trigger_check(background_tasks: BackgroundTasks):
    """Trigger an immediate Ansible check"""
    background_tasks.add_task(run_ansible_check_task)
    return {"message": "Check initiated", "status": "running"}

@app.post("/api/patch")
def trigger_patch(patch_request: PatchRequest, background_tasks: BackgroundTasks):
    """Trigger patch operation on specified hosts"""
    if not patch_request.hostnames:
        raise HTTPException(status_code=400, detail="No hostnames provided")
    
    # Verify all hosts exist
    for hostname in patch_request.hostnames:
        host = db.get_host_by_hostname(hostname)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host not found: {hostname}")
    
    background_tasks.add_task(
        run_ansible_patch_task,
        patch_request.hostnames,
        patch_request.become_password
    )
    
    return {
        "message": f"Patch initiated for {len(patch_request.hostnames)} host(s)",
        "status": "running",
        "hostnames": patch_request.hostnames
    }

@app.get("/api/history")
def get_history(hostname: Optional[str] = None, limit: int = 50):
    """Get patch history, optionally filtered by hostname"""
    try:
        if hostname:
            host = db.get_host_by_hostname(hostname)
            if not host:
                raise HTTPException(status_code=404, detail="Host not found")
            history = db.get_patch_history(host_id=host["id"], limit=limit)
        else:
            history = db.get_patch_history(limit=limit)
        
        return history
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
def get_stats():
    """Get overall statistics"""
    try:
        hosts = db.get_all_hosts()
        
        total_hosts = len(hosts)
        hosts_up_to_date = sum(1 for h in hosts if h.get("status") == "up-to-date")
        hosts_need_updates = sum(1 for h in hosts if h.get("status") == "updates-available")
        hosts_unreachable = sum(1 for h in hosts if h.get("status") == "unreachable")
        total_pending_updates = sum(h.get("total_updates", 0) for h in hosts)
        
        return {
            "total_hosts": total_hosts,
            "up_to_date": hosts_up_to_date,
            "need_updates": hosts_need_updates,
            "unreachable": hosts_unreachable,
            "total_pending_updates": total_pending_updates,
            "last_updated": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Startup event - run initial check
@app.on_event("startup")
async def startup_event():
    """Run initial check on startup"""
    print("Starting PatchPilot...")
    print("Running initial Ansible check...")
    run_ansible_check_task()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
