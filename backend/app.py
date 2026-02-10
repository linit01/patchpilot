from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import asyncio

from database import DatabaseClient
from ansible_runner import AnsibleRunner
from settings_api import router as settings_router

# WebSocket connection manager for patch progress
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

# Initialize database client
db = DatabaseClient()

# Initialize Ansible runner
ansible = AnsibleRunner(
    playbook_path="/ansible/check-os-updates.yml",
    inventory_path="/ansible/hosts",
    db_client=db
)

# Create FastAPI app
app = FastAPI(title="PatchPilot API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include settings router
app.include_router(settings_router)

# Pydantic models
class PatchRequest(BaseModel):
    hostnames: List[str]
    become_password: Optional[str] = None

# Startup event
@app.on_event("startup")
async def startup_event():
    print("Starting PatchPilot...")
    await db.connect()
    print("Database connected (DatabaseClient)")
    
    # Create database pool for Settings API
    from dependencies import create_pool
    await create_pool()
    
    # Start background task for periodic checks
    asyncio.create_task(periodic_ansible_check())
    
    # Run initial check
    asyncio.create_task(run_ansible_check_task())

@app.on_event("shutdown")
async def shutdown_event():
    await db.close()
    from dependencies import close_pool
    await close_pool()

# Background task to run Ansible check
async def run_ansible_check_task(limit_hosts: list = None):

    """Background task to run Ansible check and update database"""
    if limit_hosts:
        print(f"[DEBUG] Running check for specific hosts: {limit_hosts}")
    else:
        print(f"[DEBUG] Running check for all hosts")
    print(f"[{datetime.now()}] Running Ansible check...")
    # Ensure we're connected
    await db.connect()
    success, hosts_data = await ansible.run_check(limit_hosts=limit_hosts)
    if not success:
        print(f"Ansible check failed: {hosts_data.get('error', 'Unknown error')}")
        return
    # Update database with results
    for hostname, data in hosts_data.items():
        try:
            host = await db.upsert_host(
                hostname=hostname,
                ip_address=data.get("ip_address", ""),
                os_type=data.get("os_type", ""),
                os_family=data.get("os_family", ""),
                status=data.get("status", "unknown"),
                total_updates=data.get("total_updates", 0),
                reboot_required=data.get("reboot_required", False)
            )
            
            # Always clear old packages for this host
            if host:
                await db.delete_packages_for_host(host['id'])
                
                # Store new package details if any exist
                if data.get("update_details"):
                    # Insert new packages
                    for package in data.get("update_details", []):
                        await db.upsert_package(
                            host_id=host['id'],
                            package_name=package.get("package_name", ""),
                            current_version=package.get("current_version", ""),
                            available_version=package.get("available_version", ""),
                            update_type=data.get("update_type", "apt")
                        )
            
            print(f"Updated host: {hostname} - Status: {data.get('status')} - Updates: {data.get('total_updates')}")
        except Exception as e:
            print(f"Error updating host {hostname}: {e}")
    
    print(f"[{datetime.now()}] Ansible check completed")

# Background task to run ansible patch
async def run_ansible_patch_task(hostnames: List[str], become_password: Optional[str] = None):
    """Background task to run Ansible patch on specified hosts"""
    print(f"[{datetime.now()}] Running Ansible patch on: {', '.join(hostnames)}")
    await db.connect()
    
    # Broadcast start
    await manager.broadcast({
        "type": "start",
        "hosts": hostnames,
        "message": f"Starting patch for {len(hostnames)} host(s)..."
    })
    
    # Create progress callback for real-time updates
    async def progress_callback(message):
        await manager.broadcast({
            "type": "progress",
            "message": message
        })

    # Patch each host
    for hostname in hostnames:
        await manager.broadcast({
            "type": "progress",
            "hostname": hostname,
            "message": f"Patching {hostname}..."
        })
    
    start_time = datetime.now()
    # Create progress callback for real-time updates
    async def progress_callback(message):
        await manager.broadcast({
            "type": "progress",
            "message": message
        })
    end_time = datetime.now()
    success, results = await ansible.run_patch(
        limit_hosts=hostnames, 
        become_password=become_password,
        progress_callback=progress_callback
    )
    
    if success:
        print(f"[{end_time}] Ansible patch completed successfully")
        print(f"PATCH OUTPUT: {results.get('output', 'No output')[:2000]}")
        await manager.broadcast({
            "type": "success",
            "message": "Patching completed successfully. Refreshing status..."
        })
        # Re-run check to update status
        await run_ansible_check_task()
        await manager.broadcast({
            "type": "complete",
            "message": "All operations complete!"
        })
    else:
        print(f"[{end_time}] Ansible patch failed: {results.get('error', 'Unknown error')}")
        print(f"STDERR: {results.get('stderr', 'No stderr')}")
        print(f"STDOUT: {results.get('stdout', 'No stdout')}")
        await manager.broadcast({
            "type": "error",
            "message": f"Patch failed: {results.get('error', 'Unknown error')}"
        })

# Periodic check task
async def periodic_ansible_check():
    """Run Ansible check periodically"""
    while True:
        await asyncio.sleep(120)
        await run_ansible_check_task()

# API Endpoints
# WebSocket endpoint for real-time patch progress
@app.websocket("/ws/patch-progress")
async def websocket_patch_progress(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/")
async def root():
    return {"message": "PatchPilot API", "version": "1.0.0"}

@app.get("/api/hosts")
async def get_hosts():
    """Get all hosts with their update status"""
    hosts = await db.get_all_hosts()
    return hosts

@app.get("/api/hosts/{hostname}")
async def get_host(hostname: str):
    """Get details for a specific host"""
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    return host

@app.get("/api/hosts/{hostname}/packages")
async def get_host_packages(hostname: str):
    """Get pending updates for a specific host"""
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    
    packages = await db.get_packages_for_host(host['id'])
    return packages

@app.get("/api/stats")
async def get_stats():
    """Get summary statistics"""
    stats = await db.get_stats()
    return stats

@app.post("/api/check")
async def trigger_check(background_tasks: BackgroundTasks):
    """Trigger an immediate Ansible check"""
    background_tasks.add_task(run_ansible_check_task)
    return {"message": "Check initiated", "status": "running"}

@app.post("/api/check/{hostname}")
async def trigger_single_host_check(hostname: str, background_tasks: BackgroundTasks):
    """Trigger an immediate Ansible check for a single host"""
    # Verify host exists
    host = await db.get_host_by_hostname(hostname)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    
    background_tasks.add_task(run_ansible_check_task, [hostname])
    return {"message": f"Check initiated for {hostname}", "status": "running"}

@app.post("/api/patch")
async def trigger_patch(patch_request: PatchRequest, background_tasks: BackgroundTasks):
    """Trigger patching for specific hosts"""
    if not patch_request.hostnames:
        raise HTTPException(status_code=400, detail="No hostnames provided")
    
    # Verify all hosts exist
    for hostname in patch_request.hostnames:
        host = await db.get_host_by_hostname(hostname)
        if not host:
            raise HTTPException(status_code=404, detail=f"Host {hostname} not found")
    
    background_tasks.add_task(
        run_ansible_patch_task, 
        patch_request.hostnames,
        patch_request.become_password
    )
    return {
        "message": "Patch initiated",
        "status": "running",
        "hosts": patch_request.hostnames
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
