"""
PatchPilot - Settings API
Handles host management, SSH credentials, and application settings
"""

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel, Field, validator
from typing import Optional, List
import asyncpg
import logging
import socket
import paramiko
import io
from datetime import datetime
import httpx
import uuid
import os

from encryption_utils import encrypt_credential, decrypt_credential, validate_ssh_key
from dependencies import get_db_pool
from sync_ansible_inventory import sync_ansible_inventory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================================
# Pydantic Models
# ============================================================================

class HostCreate(BaseModel):
    """Model for creating a new host"""
    hostname: str = Field(..., min_length=1, max_length=255, description="Hostname or IP address")
    ssh_user: str = Field(default="root", max_length=100, description="SSH username")
    ssh_port: int = Field(default=22, ge=1, le=65535, description="SSH port number")
    ssh_key_type: str = Field(default="default", description="SSH auth type: default, pasted, file, password")
    ssh_private_key: Optional[str] = Field(None, description="SSH private key content (for pasted/file)")
    ssh_password: Optional[str] = Field(None, description="SSH password (not recommended)")
    notes: Optional[str] = Field(None, max_length=1000, description="Optional notes")
    tags: Optional[str] = Field(None, max_length=255, description="Comma-separated tags")
    
    @validator('ssh_key_type')
    def validate_key_type(cls, v):
        valid_types = ['default', 'pasted', 'file', 'password']
        if v not in valid_types:
            raise ValueError(f"ssh_key_type must be one of: {', '.join(valid_types)}")
        return v
    
    @validator('ssh_private_key')
    def validate_private_key(cls, v, values):
        if v and values.get('ssh_key_type') in ['pasted', 'file']:
            is_valid, message = validate_ssh_key(v)
            if not is_valid:
                raise ValueError(f"Invalid SSH key: {message}")
        return v


class HostUpdate(BaseModel):
    """Model for updating an existing host"""
    hostname: Optional[str] = Field(None, min_length=1, max_length=255)
    ssh_user: Optional[str] = Field(None, max_length=100)
    ssh_port: Optional[int] = Field(None, ge=1, le=65535)
    ssh_key_type: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_password: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=1000)
    tags: Optional[str] = Field(None, max_length=255)
    allow_auto_reboot: Optional[bool] = None


class HostResponse(BaseModel):
    """Model for host data in API responses"""
    id: uuid.UUID
    hostname: str
    ssh_user: str
    ssh_port: int
    ssh_key_type: str
    has_ssh_key: bool = Field(description="Whether SSH key is configured")
    has_password: bool = Field(description="Whether password is configured")
    notes: Optional[str]
    tags: Optional[str]
    status: Optional[str]
    is_control_node: bool
    last_checked: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        json_encoders = {
            uuid.UUID: str
        }

# Saved SSH Keys Models
class SavedSSHKeyCreate(BaseModel):
    """Model for creating a saved SSH key"""
    name: str = Field(..., min_length=1, max_length=100)
    ssh_key: str = Field(..., min_length=1)
    is_default: Optional[bool] = False

class SavedSSHKeyUpdate(BaseModel):
    """Model for updating a saved SSH key"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    ssh_key: Optional[str] = None
    is_default: Optional[bool] = None

class SavedSSHKeyResponse(BaseModel):
    """Model for saved SSH key in API responses (key content excluded)"""
    id: uuid.UUID
    name: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class TestConnectionRequest(BaseModel):
    """Model for testing SSH connection"""
    hostname: str
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_type: str = "default"
    ssh_private_key: Optional[str] = None
    ssh_password: Optional[str] = None


class TestConnectionResponse(BaseModel):
    """Response from connection test"""
    success: bool
    message: str
    details: Optional[dict] = None


class BulkImportRequest(BaseModel):
    """Model for bulk import"""
    format: str = Field(..., description="Import format: ansible, csv, json")
    data: str = Field(..., description="Import data content")
    overwrite: bool = Field(default=False, description="Overwrite existing hosts")


class BulkExportResponse(BaseModel):
    """Response for bulk export"""
    format: str
    data: str
    count: int


# ============================================================================
# Database Helpers
# ============================================================================

async def log_audit_action(pool: asyncpg.Pool, action: str, resource_type: str, 
                           resource_id: str, details: dict = None):
    """Log an audit trail entry"""
    import json
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO audit_log (action, resource_type, resource_id, details)
            VALUES ($1, $2, $3, $4)
        """, action, resource_type, resource_id, json.dumps(details or {}))


# ============================================================================
# Host Management Endpoints
# ============================================================================

@router.get("/hosts", response_model=List[HostResponse])
async def list_hosts(pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    List all configured hosts.
    
    Returns:
        List of hosts with their configuration (credentials excluded)
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node,
                last_checked, created_at, updated_at
            FROM hosts
            ORDER BY hostname
        """)
        
        return [dict(row) for row in rows]


@router.post("/hosts", response_model=HostResponse, status_code=201)
async def create_host(host: HostCreate, pool: asyncpg.Pool = Depends(get_db_pool), background_tasks: BackgroundTasks = None):
    """
    Create a new host configuration.
    
    Args:
        host: Host configuration details
        
    Returns:
        Created host details
        
    Raises:
        HTTPException: If hostname already exists or validation fails
    """
    # Check if hostname already exists
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM hosts WHERE hostname = $1", 
            host.hostname
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Host '{host.hostname}' already exists")
        
        # Encrypt credentials if provided
        ssh_key_encrypted = None
        ssh_password_encrypted = None
        
        if host.ssh_private_key and host.ssh_key_type in ['pasted', 'file']:
            try:
                ssh_key_encrypted = encrypt_credential(host.ssh_private_key)
            except Exception as e:
                logger.error(f"Failed to encrypt SSH key: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        if host.ssh_password and host.ssh_key_type == 'password':
            try:
                ssh_password_encrypted = encrypt_credential(host.ssh_password)
            except Exception as e:
                logger.error(f"Failed to encrypt password: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt password")
        
        # Insert host
        row = await conn.fetchrow("""
            INSERT INTO hosts (
                hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted, ssh_password_encrypted,
                notes, tags
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node,
                last_checked, created_at, updated_at
        """, host.hostname, host.ssh_user, host.ssh_port, host.ssh_key_type,
            ssh_key_encrypted, ssh_password_encrypted, host.notes, host.tags)
        
        # Log audit trail
        await log_audit_action(pool, "CREATE", "host", host.hostname, {
            "ssh_user": host.ssh_user,
            "ssh_port": host.ssh_port,
            "ssh_key_type": host.ssh_key_type
        })
        
        # Sync to Ansible inventory
        try:
            inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
            await sync_ansible_inventory(pool, inventory_path)
            logger.info(f"Synced new host {host.hostname} to Ansible inventory")
        except Exception as e:
            logger.error(f"Failed to sync Ansible inventory: {e}")
            # Don't fail the request, just log the error
        
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"http://localhost:8000/api/check/{host.hostname}")
                logger.info(f"Triggered check for new host: {host.hostname}")
        except Exception as e:
            logger.warning(f"Failed to trigger check for {host.hostname}: {e}")
            # Don't fail the request

        logger.info(f"Created new host: {host.hostname}")
        return dict(row)


@router.get("/hosts/{host_id}", response_model=HostResponse)
async def get_host(host_id: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Get details for a specific host.
    
    Args:
        host_id: Host UUID
        
    Returns:
        Host details (credentials excluded)
        
    Raises:
        HTTPException: If host not found
    """
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node,
                last_checked, created_at, updated_at
            FROM hosts
            WHERE id = $1
        """, host_uuid)
        
        if not row:
            raise HTTPException(status_code=404, detail=f"Host with ID {host_id} not found")
        
        return dict(row)


@router.put("/hosts/{host_id}", response_model=HostResponse)
async def update_host(host_id: str, host: HostUpdate, pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Update an existing host configuration.
    
    Args:
        host_id: Host UUID to update
        host: Updated host details
        
    Returns:
        Updated host details
        
    Raises:
        HTTPException: If host not found or validation fails
    """
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        # Check host exists
        existing = await conn.fetchrow("SELECT * FROM hosts WHERE id = $1", host_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Host with ID {host_id} not found")
        
        # Build update query dynamically
        updates = []
        values = []
        param_idx = 1
        
        if host.hostname is not None:
            updates.append(f"hostname = ${param_idx}")
            values.append(host.hostname)
            param_idx += 1
        
        if host.ssh_user is not None:
            updates.append(f"ssh_user = ${param_idx}")
            values.append(host.ssh_user)
            param_idx += 1
        
        if host.ssh_port is not None:
            updates.append(f"ssh_port = ${param_idx}")
            values.append(host.ssh_port)
            param_idx += 1
        
        if host.ssh_key_type is not None:
            updates.append(f"ssh_key_type = ${param_idx}")
            values.append(host.ssh_key_type)
            param_idx += 1
        
        if host.ssh_private_key is not None and host.ssh_private_key.strip():
            ssh_key_encrypted = encrypt_credential(host.ssh_private_key)
            updates.append(f"ssh_private_key_encrypted = ${param_idx}")
            values.append(ssh_key_encrypted)
            param_idx += 1
        
        if host.ssh_password is not None and host.ssh_password.strip():
            ssh_password_encrypted = encrypt_credential(host.ssh_password)
            updates.append(f"ssh_password_encrypted = ${param_idx}")
            values.append(ssh_password_encrypted)
            param_idx += 1
        
        if host.notes is not None:
            updates.append(f"notes = ${param_idx}")
            values.append(host.notes)
            param_idx += 1
        
        if host.tags is not None:
            updates.append(f"tags = ${param_idx}")
            values.append(host.tags)
            param_idx += 1

        if host.allow_auto_reboot is not None:
            updates.append(f"allow_auto_reboot = ${param_idx}")
            values.append(host.allow_auto_reboot)
            param_idx += 1
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Add host_uuid as last parameter
        values.append(host_uuid)
        
        # Execute update
        query = f"""
            UPDATE hosts 
            SET {', '.join(updates)}
            WHERE id = ${param_idx}
            RETURNING 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node, allow_auto_reboot,
                last_checked, created_at, updated_at
        """
        
        row = await conn.fetchrow(query, *values)
        
        # Log audit trail
        await log_audit_action(pool, "UPDATE", "host", existing['hostname'], dict(host))
        
        # Sync to Ansible inventory
        try:
            inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
            await sync_ansible_inventory(pool, inventory_path)
            logger.info(f"Synced updated host {existing['hostname']} to Ansible inventory")
        except Exception as e:
            logger.error(f"Failed to sync Ansible inventory: {e}")
        
        logger.info(f"Updated host: {existing['hostname']}")
        return dict(row)


@router.delete("/hosts/{host_id}", status_code=204)
async def delete_host(host_id: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Delete a host configuration.
    
    Args:
        host_id: Host UUID to delete
        
    Raises:
        HTTPException: If host not found
    """
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        # Get hostname for audit log
        hostname = await conn.fetchval("SELECT hostname FROM hosts WHERE id = $1", host_uuid)
        if not hostname:
            raise HTTPException(status_code=404, detail=f"Host with ID {host_id} not found")
        
        # Delete host
        await conn.execute("DELETE FROM hosts WHERE id = $1", host_uuid)
        
        # Log audit trail
        await log_audit_action(pool, "DELETE", "host", hostname, {"host_id": str(host_uuid)})
        
        # Sync to Ansible inventory
        try:
            inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
            await sync_ansible_inventory(pool, inventory_path)
            logger.info(f"Synced after deleting host {hostname} from Ansible inventory")
        except Exception as e:
            logger.error(f"Failed to sync Ansible inventory: {e}")
        
        logger.info(f"Deleted host: {hostname}")


# ============================================================================
# Connection Testing
# ============================================================================

@router.post("/hosts/test", response_model=TestConnectionResponse)
async def test_connection(request: TestConnectionRequest):
    """
    Test SSH connection to a host.
    
    Args:
        request: Connection details to test
        
    Returns:
        Test result with success status and message
    """
    try:
        # Create SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Prepare authentication
        connect_kwargs = {
            "hostname": request.hostname,
            "port": request.ssh_port,
            "username": request.ssh_user,
            "timeout": 10,
        }
        
        if request.ssh_key_type == "password" and request.ssh_password:
            connect_kwargs["password"] = request.ssh_password
        elif request.ssh_key_type in ["pasted", "file"] and request.ssh_private_key:
            # Load private key from string
            key_file = io.StringIO(request.ssh_private_key)
            try:
                pkey = paramiko.RSAKey.from_private_key(key_file)
            except:
                try:
                    key_file.seek(0)
                    pkey = paramiko.Ed25519Key.from_private_key(key_file)
                except:
                    key_file.seek(0)
                    pkey = paramiko.ECDSAKey.from_private_key(key_file)
            connect_kwargs["pkey"] = pkey
        # else: use default SSH keys from ~/.ssh/
        
        # Attempt connection
        client.connect(**connect_kwargs)
        
        # Test command execution
        stdin, stdout, stderr = client.exec_command("uname -a")
        uname_output = stdout.read().decode().strip()
        
        client.close()
        
        logger.info(f"SSH connection test successful: {request.hostname}")
        return TestConnectionResponse(
            success=True,
            message=f"Successfully connected to {request.hostname}",
            details={
                "hostname": request.hostname,
                "port": request.ssh_port,
                "user": request.ssh_user,
                "system_info": uname_output
            }
        )
        
    except paramiko.AuthenticationException:
        logger.warning(f"SSH authentication failed: {request.hostname}")
        return TestConnectionResponse(
            success=False,
            message="Authentication failed - check username, password, or SSH key"
        )
    except paramiko.SSHException as e:
        logger.warning(f"SSH connection failed: {request.hostname} - {e}")
        return TestConnectionResponse(
            success=False,
            message=f"SSH connection failed: {str(e)}"
        )
    except socket.timeout:
        logger.warning(f"Connection timeout: {request.hostname}")
        return TestConnectionResponse(
            success=False,
            message=f"Connection timeout - host may be unreachable on port {request.ssh_port}"
        )
    except Exception as e:
        logger.error(f"Connection test error: {e}")
        return TestConnectionResponse(
            success=False,
            message=f"Connection test failed: {str(e)}"
        )


# ============================================================================
# Bulk Operations
# ============================================================================

@router.post("/hosts/import")
async def import_hosts(request: BulkImportRequest, pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Bulk import hosts from various formats.
    
    Supported formats:
    - ansible: Ansible inventory file format
    - csv: CSV with columns: hostname,ssh_user,ssh_port,notes
    - json: JSON array of host objects
    
    Args:
        request: Import request with format and data
        
    Returns:
        Import summary with counts of added/updated/failed hosts
    """
    # Implementation would parse the data format and bulk insert
    # This is a placeholder for the implementation
    raise HTTPException(status_code=501, detail="Bulk import not yet implemented")


@router.get("/hosts/export", response_model=BulkExportResponse)
async def export_hosts(format: str = "json", pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Export all hosts to specified format.
    
    Args:
        format: Export format (json, csv, ansible)
        
    Returns:
        Exported data in requested format
    """
    # Implementation would format hosts data for export
    # This is a placeholder for the implementation
    raise HTTPException(status_code=501, detail="Export not yet implemented")


# ============================================================================
# Application Settings
# ============================================================================

@router.get("/app")
async def get_app_settings(pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get application-wide settings"""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value, description FROM settings ORDER BY key")
        return {row['key']: {"value": row['value'], "description": row['description']} for row in rows}


@router.put("/app/{key}")
async def update_app_setting(key: str, value: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """Update an application setting"""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE settings 
            SET value = $1, updated_at = CURRENT_TIMESTAMP
            WHERE key = $2
        """, value, key)
        
        await log_audit_action(pool, "UPDATE", "setting", key, {"value": value})
        
        return {"key": key, "value": value}


# ============================================================================
# Ansible Inventory Sync
# ============================================================================

@router.post("/sync-inventory")
async def sync_inventory(pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Manually trigger sync of database hosts to Ansible inventory.
    Useful for fixing sync issues or after bulk imports.
    """
    try:
        inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
        count = await sync_ansible_inventory(pool, inventory_path)
        return {
            "success": True,
            "message": f"Synced {count} hosts to Ansible inventory",
            "inventory_path": inventory_path,
            "hosts_synced": count
        }
    except Exception as e:
        logger.error(f"Manual inventory sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.get("/system-info")
async def get_system_info():
    """
    Get system information including Ansible version, Python version, etc.
    """
    import subprocess
    import sys
    
    try:
        # Get Ansible version
        ansible_result = subprocess.run(
            ["ansible", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        ansible_version = ansible_result.stdout.split('\n')[0] if ansible_result.returncode == 0 else "Not installed"
        
        # Get Python version
        python_version = f"Python {sys.version.split()[0]}"
        
        # Get encryption status
        encryption_key = os.getenv("PATCHPILOT_ENCRYPTION_KEY")
        encryption_enabled = bool(encryption_key and encryption_key.strip())
        
        return {
            "ansible_version": ansible_version,
            "python_version": python_version,
            "encryption_enabled": encryption_enabled,
            "database_connected": True  # If we got here, DB is connected
        }
    except subprocess.TimeoutExpired:
        return {
            "ansible_version": "Detection timeout",
            "python_version": f"Python {sys.version.split()[0]}",
            "encryption_enabled": bool(os.getenv("PATCHPILOT_ENCRYPTION_KEY")),
            "database_connected": True
        }
    except Exception as e:
        logger.error(f"Failed to get system info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get system info: {str(e)}")

# ============================================================================
# Saved SSH Keys Endpoints
# ============================================================================

@router.get("/ssh-keys", response_model=List[SavedSSHKeyResponse])
async def list_saved_ssh_keys(pool: asyncpg.Pool = Depends(get_db_pool)):
    """List all saved SSH keys (without key content)"""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, is_default, created_at, updated_at
            FROM saved_ssh_keys
            ORDER BY is_default DESC, name ASC
        """)
        return [dict(row) for row in rows]

@router.get("/ssh-keys/{key_id}", response_model=SavedSSHKeyResponse)
async def get_saved_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get a specific saved SSH key (without key content)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, is_default, created_at, updated_at
            FROM saved_ssh_keys
            WHERE id = $1
        """, key_uuid)
        
        if not row:
            raise HTTPException(status_code=404, detail=f"SSH key with ID {key_id} not found")
        
        return dict(row)

@router.post("/ssh-keys", response_model=SavedSSHKeyResponse, status_code=201)
async def create_saved_ssh_key(key: SavedSSHKeyCreate, pool: asyncpg.Pool = Depends(get_db_pool)):
    """Create a new saved SSH key"""
    # Check if name already exists
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM saved_ssh_keys WHERE name = $1",
            key.name
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"SSH key '{key.name}' already exists")
        
        # Encrypt the SSH key
        try:
            ssh_key_encrypted = encrypt_credential(key.ssh_key)
        except Exception as e:
            logger.error(f"Failed to encrypt SSH key: {e}")
            raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        # If this should be default, unset other defaults
        if key.is_default:
            await conn.execute("UPDATE saved_ssh_keys SET is_default = FALSE")
        
        # Insert the key
        row = await conn.fetchrow("""
            INSERT INTO saved_ssh_keys (name, ssh_key_encrypted, is_default)
            VALUES ($1, $2, $3)
            RETURNING id, name, is_default, created_at, updated_at
        """, key.name, ssh_key_encrypted, key.is_default)
        
        logger.info(f"Created saved SSH key: {key.name}")
        return dict(row)

@router.put("/ssh-keys/{key_id}", response_model=SavedSSHKeyResponse)
async def update_saved_ssh_key(
    key_id: str,
    key: SavedSSHKeyUpdate,
    pool: asyncpg.Pool = Depends(get_db_pool)
):
    """Update a saved SSH key"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        # Check if key exists
        existing = await conn.fetchrow("SELECT id, name FROM saved_ssh_keys WHERE id = $1", key_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail=f"SSH key with ID {key_id} not found")
        
        # Build update query dynamically
        updates = []
        values = []
        param_count = 1
        
        if key.name is not None:
            # Check name uniqueness
            name_exists = await conn.fetchval(
                "SELECT id FROM saved_ssh_keys WHERE name = $1 AND id != $2",
                key.name, key_uuid
            )
            if name_exists:
                raise HTTPException(status_code=409, detail=f"SSH key '{key.name}' already exists")
            
            updates.append(f"name = ${param_count}")
            values.append(key.name)
            param_count += 1
        
        if key.ssh_key is not None:
            try:
                ssh_key_encrypted = encrypt_credential(key.ssh_key)
                updates.append(f"ssh_key_encrypted = ${param_count}")
                values.append(ssh_key_encrypted)
                param_count += 1
            except Exception as e:
                logger.error(f"Failed to encrypt SSH key: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        if key.is_default is not None:
            if key.is_default:
                # Unset other defaults
                await conn.execute("UPDATE saved_ssh_keys SET is_default = FALSE WHERE id != $1", key_uuid)
            
            updates.append(f"is_default = ${param_count}")
            values.append(key.is_default)
            param_count += 1
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Add updated_at
        updates.append(f"updated_at = CURRENT_TIMESTAMP")
        
        # Add WHERE clause parameter
        values.append(key_uuid)
        
        # Execute update
        row = await conn.fetchrow(f"""
            UPDATE saved_ssh_keys
            SET {', '.join(updates)}
            WHERE id = ${param_count}
            RETURNING id, name, is_default, created_at, updated_at
        """, *values)
        
        logger.info(f"Updated saved SSH key: {existing['name']}")
        return dict(row)

@router.delete("/ssh-keys/{key_id}")
async def delete_saved_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """Delete a saved SSH key"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        result = await conn.fetchrow("""
            DELETE FROM saved_ssh_keys
            WHERE id = $1
            RETURNING name
        """, key_uuid)
        
        if not result:
            raise HTTPException(status_code=404, detail=f"SSH key with ID {key_id} not found")
        
        logger.info(f"Deleted saved SSH key: {result['name']}")
        return {"message": f"SSH key '{result['name']}' deleted successfully"}

@router.get("/ssh-keys/{key_id}/decrypt")
async def get_decrypted_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get the decrypted SSH key content (for host creation)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT ssh_key_encrypted
            FROM saved_ssh_keys
            WHERE id = $1
        """, key_uuid)
        
        if not row:
            raise HTTPException(status_code=404, detail=f"SSH key with ID {key_id} not found")
        
        try:
            decrypted_key = decrypt_credential(row['ssh_key_encrypted'])
            return {"ssh_key": decrypted_key}
        except Exception as e:
            logger.error(f"Failed to decrypt SSH key: {e}")
            raise HTTPException(status_code=500, detail="Failed to decrypt SSH key")
