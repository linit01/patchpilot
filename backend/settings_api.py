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
from auth import require_auth, require_full_admin, require_write, ownership_filter
from rbac import (owner_id, owner_id_or_param, verify_host_ownership,
                  verify_host_ownership_by_hostname, verify_ssh_key_ownership)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


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
    is_control_node: Optional[bool] = False
    allow_auto_reboot: Optional[bool] = True
    
    @validator('ssh_key_type')
    def validate_key_type(cls, v):
        valid_types = ['default', 'pasted', 'file', 'password']
        # Allow saved: prefix (frontend converts to pasted, but be tolerant)
        if v and v.startswith('saved:'):
            return 'pasted'
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
    is_control_node: Optional[bool] = None
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
    allow_auto_reboot: bool = True
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
    host_id: Optional[str] = None  # Existing host ID - use stored key if no key provided


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
async def list_hosts(owner: str = None,
                     pool: asyncpg.Pool = Depends(get_db_pool),
                     user: dict = Depends(require_auth)):
    """
    List configured hosts, scoped by role.
    full_admin: all hosts (optional ?owner= filter).  admin: own hosts only.  viewer: all hosts.
    """
    uid = owner_id_or_param(user, owner)
    async with pool.acquire() as conn:
        if uid is not None:
            rows = await conn.fetch("""
                SELECT 
                    h.id, h.hostname, h.ssh_user, h.ssh_port, h.ssh_key_type,
                    h.ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                    h.ssh_password_encrypted IS NOT NULL AS has_password,
                    h.notes, h.tags, h.status, h.is_control_node, h.allow_auto_reboot,
                    h.last_checked, h.created_at, h.updated_at,
                    u.username AS owner_username
                FROM hosts h
                LEFT JOIN users u ON h.created_by = u.id
                WHERE h.created_by = $1
                ORDER BY h.hostname
            """, uid)
        else:
            rows = await conn.fetch("""
                SELECT 
                    h.id, h.hostname, h.ssh_user, h.ssh_port, h.ssh_key_type,
                    h.ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                    h.ssh_password_encrypted IS NOT NULL AS has_password,
                    h.notes, h.tags, h.status, h.is_control_node, h.allow_auto_reboot,
                    h.last_checked, h.created_at, h.updated_at,
                    u.username AS owner_username
                FROM hosts h
                LEFT JOIN users u ON h.created_by = u.id
                ORDER BY h.hostname
            """)
        
        return [dict(row) for row in rows]


@router.post("/hosts", response_model=HostResponse, status_code=201)
async def create_host(host: HostCreate,
                      pool: asyncpg.Pool = Depends(get_db_pool),
                      background_tasks: BackgroundTasks = None,
                      user: dict = Depends(require_write)):
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
        
        # Encrypt credentials if provided.
        # encrypt_credential() returns a base64 str; encode to UTF-8 bytes for BYTEA column.
        ssh_key_encrypted = None
        ssh_password_encrypted = None
        
        if host.ssh_private_key and host.ssh_key_type in ['pasted', 'file']:
            try:
                ssh_key_encrypted = encrypt_credential(host.ssh_private_key).encode('utf-8')
            except Exception as e:
                logger.error(f"Failed to encrypt SSH key: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        if host.ssh_password and host.ssh_key_type == 'password':
            try:
                ssh_password_encrypted = encrypt_credential(host.ssh_password).encode('utf-8')
            except Exception as e:
                logger.error(f"Failed to encrypt password: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt password")
        
        # Insert host
        row = await conn.fetchrow("""
            INSERT INTO hosts (
                hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted, ssh_password_encrypted,
                notes, tags, is_control_node, allow_auto_reboot, created_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node, allow_auto_reboot,
                last_checked, created_at, updated_at
        """, host.hostname, host.ssh_user, host.ssh_port, host.ssh_key_type,
            ssh_key_encrypted, ssh_password_encrypted, host.notes, host.tags,
            host.is_control_node, host.allow_auto_reboot, user['id'])
        
        # Log audit trail (wrapped — must never block the response)
        try:
            await log_audit_action(pool, "CREATE", "host", host.hostname, {
                "ssh_user": host.ssh_user,
                "ssh_port": host.ssh_port,
                "ssh_key_type": host.ssh_key_type
            })
        except Exception as _audit_err:
            logger.warning(f"Audit log failed (non-fatal): {_audit_err}")
        
        # Sync to Ansible inventory
        try:
            inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
            await sync_ansible_inventory(pool, inventory_path)
            logger.info(f"Synced new host {host.hostname} to Ansible inventory")
        except Exception as e:
            logger.error(f"Failed to sync Ansible inventory: {e}")
            # Don't fail the request, just log the error
        
        try:
            # Trigger check directly (not via HTTP, which would need auth cookies)
            import asyncio
            from app import run_ansible_check_task
            asyncio.create_task(run_ansible_check_task([host.hostname]))
            logger.info(f"Triggered check for new host: {host.hostname}")
        except Exception as e:
            logger.warning(f"Failed to trigger check for {host.hostname}: {e}")
            # Don't fail the request

        logger.info(f"Created new host: {host.hostname}")
        return dict(row)


@router.get("/hosts/export")
async def export_hosts(format: str = "json", owner: str = None,
                       pool: asyncpg.Pool = Depends(get_db_pool),
                       user: dict = Depends(require_auth)):
    """Export hosts to specified format, scoped by role."""
    import json as json_lib
    import csv
    import io
    
    uid = owner_id_or_param(user, owner)
    async with pool.acquire() as conn:
        if uid is not None:
            rows = await conn.fetch("""
                SELECT hostname, ssh_user, ssh_port, tags, notes, 
                       is_control_node, allow_auto_reboot, status, os_family, ip_address
                FROM hosts WHERE created_by = $1 ORDER BY hostname
            """, uid)
        else:
            rows = await conn.fetch("""
                SELECT hostname, ssh_user, ssh_port, tags, notes, 
                       is_control_node, allow_auto_reboot, status, os_family, ip_address
                FROM hosts ORDER BY hostname
            """)
        
        hosts = [dict(r) for r in rows]
    
    if format == "json":
        # Convert to JSON-safe format
        for h in hosts:
            h['is_control_node'] = bool(h.get('is_control_node', False))
            h['allow_auto_reboot'] = bool(h.get('allow_auto_reboot', True))
        
        return {
            "format": "json",
            "data": json_lib.dumps(hosts, indent=2, default=str),
            "count": len(hosts)
        }
    
    elif format == "csv":
        output = io.StringIO()
        fieldnames = ["hostname", "ssh_user", "ssh_port", "tags", "notes", 
                      "is_control_node", "allow_auto_reboot", "status", "os_family", "ip_address"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for h in hosts:
            writer.writerow({k: h.get(k, '') for k in fieldnames})
        
        return {
            "format": "csv",
            "data": output.getvalue(),
            "count": len(hosts)
        }
    
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}. Use 'json' or 'csv'.")

@router.get("/hosts/{host_id}", response_model=HostResponse)
async def get_host(host_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                   user: dict = Depends(require_auth)):
    """Get details for a specific host (ownership-scoped)."""
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        if not await verify_host_ownership(conn, user, host_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this host")
        row = await conn.fetchrow("""
            SELECT 
                id, hostname, ssh_user, ssh_port, ssh_key_type,
                ssh_private_key_encrypted IS NOT NULL AS has_ssh_key,
                ssh_password_encrypted IS NOT NULL AS has_password,
                notes, tags, status, is_control_node, allow_auto_reboot,
                last_checked, created_at, updated_at
            FROM hosts
            WHERE id = $1
        """, host_uuid)
        
        if not row:
            raise HTTPException(status_code=404, detail=f"Host with ID {host_id} not found")
        
        return dict(row)


@router.put("/hosts/{host_id}", response_model=HostResponse)
async def update_host(host_id: str, host: HostUpdate, pool: asyncpg.Pool = Depends(get_db_pool),
                      user: dict = Depends(require_write)):
    """Update an existing host configuration (ownership-scoped, write-only)."""
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        # Check host exists + ownership
        existing = await conn.fetchrow("SELECT * FROM hosts WHERE id = $1", host_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Host with ID {host_id} not found")
        if not await verify_host_ownership(conn, user, host_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this host")
        
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
        
        # Resolve saved:UUID key references coming from the edit form.
        # We bypass encrypt_credential entirely and write the raw Fernet bytes straight
        # into hosts.ssh_private_key_encrypted (BYTEA) — same key, no conversion loss.
        if host.ssh_key_type and host.ssh_key_type.startswith('saved:'):
            saved_key_id = host.ssh_key_type.replace('saved:', '')
            try:
                import base64 as _b64
                saved_key_uuid = uuid.UUID(saved_key_id)
                key_row = await conn.fetchrow(
                    "SELECT ssh_key_encrypted FROM saved_ssh_keys WHERE id = $1", saved_key_uuid
                )
                if key_row:
                    raw_val = key_row['ssh_key_encrypted']
                    # Normalise to bytes regardless of TEXT (base64 str) or BYTEA source
                    if isinstance(raw_val, str):
                        raw_key_bytes = _b64.b64decode(raw_val.encode('utf-8'))
                    elif isinstance(raw_val, memoryview):
                        raw_key_bytes = bytes(raw_val)
                    else:
                        raw_key_bytes = raw_val

                    # Write directly — add to updates list bypassing the normal key-encrypt path
                    updates.append(f"ssh_private_key_encrypted = ${param_idx}")
                    values.append(raw_key_bytes)
                    param_idx += 1
                    updates.append(f"ssh_key_type = ${param_idx}")
                    values.append('pasted')
                    param_idx += 1
                    host = host.copy(update={'ssh_key_type': None, 'ssh_private_key': None})
                    logger.info(f"Resolved saved key {saved_key_id} for host update")
                else:
                    logger.warning(f"Saved key {saved_key_id} not found; skipping key update")
                    host = host.copy(update={'ssh_key_type': None, 'ssh_private_key': None})
            except Exception as _e:
                logger.error(f"Failed to resolve saved key for update: {_e}")
                host = host.copy(update={'ssh_key_type': None, 'ssh_private_key': None})

        if host.ssh_private_key is not None and host.ssh_private_key.strip():
            try:
                ssh_key_encrypted = encrypt_credential(host.ssh_private_key).encode('utf-8')
            except Exception as _enc_e:
                logger.error(f"Failed to encrypt SSH key for host update: {_enc_e}")
                raise HTTPException(status_code=400, detail=f"Invalid SSH key: {_enc_e}")
            updates.append(f"ssh_private_key_encrypted = ${param_idx}")
            values.append(ssh_key_encrypted)
            param_idx += 1
        
        if host.ssh_password is not None and host.ssh_password.strip():
            ssh_password_encrypted = encrypt_credential(host.ssh_password).encode('utf-8')
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

        if host.is_control_node is not None:
            updates.append(f"is_control_node = ${param_idx}")
            values.append(host.is_control_node)
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
        
        # Log audit trail (wrapped in try/except — must never block the response)
        try:
            safe_details = {k: str(v) if v is not None else None for k, v in host.__dict__.items()
                            if not k.startswith('__') and k not in ('ssh_private_key', 'ssh_password')}
            await log_audit_action(pool, "UPDATE", "host", existing['hostname'], safe_details)
        except Exception as _audit_err:
            logger.warning(f"Audit log failed (non-fatal): {_audit_err}")
        
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
async def delete_host(host_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                      user: dict = Depends(require_write)):
    """Delete a host configuration (ownership-scoped, write-only)."""
    try:
        host_uuid = uuid.UUID(host_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid host ID format")
    
    async with pool.acquire() as conn:
        if not await verify_host_ownership(conn, user, host_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this host")
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
async def test_connection(request: TestConnectionRequest, pool: asyncpg.Pool = Depends(get_db_pool),
                          user: dict = Depends(require_write)):
    """
    Test SSH connection to a host using the ssh command directly.
    This matches how Ansible connects, avoiding paramiko key format issues.
    """
    import subprocess
    import tempfile
    
    # v4.8 — unmistakable debug output
    print(f"=== TEST CONNECTION v4.9 ===")
    print(f"  hostname={request.hostname}, user={request.ssh_user}, port={request.ssh_port}")
    print(f"  key_type='{request.ssh_key_type}'")
    print(f"  has_private_key={bool(request.ssh_private_key)}, key_len={len(request.ssh_private_key) if request.ssh_private_key else 0}")
    
    tmp_key_file = None
    ssh_key_content = request.ssh_private_key
    effective_key_type = request.ssh_key_type

    # Handle 'default' key type — resolve to the user's saved default key
    if request.ssh_key_type == 'default':
        print(f"  Resolving 'default' key type to saved default key...")
        try:
            async with pool.acquire() as conn:
                # Try user's own default first, then fall back to any default
                row = await conn.fetchrow(
                    "SELECT ssh_key_encrypted FROM saved_ssh_keys WHERE is_default = TRUE AND created_by = $1 LIMIT 1",
                    user['id']
                )
                if not row:
                    row = await conn.fetchrow(
                        "SELECT ssh_key_encrypted FROM saved_ssh_keys WHERE is_default = TRUE LIMIT 1"
                    )
                if row and row['ssh_key_encrypted']:
                    ssh_key_content = decrypt_credential(row['ssh_key_encrypted'])
                    effective_key_type = 'pasted'
                    print(f"  Resolved default key, length={len(ssh_key_content)}")
                else:
                    print(f"  No default saved key found in database")
                    return TestConnectionResponse(success=False,
                        message="No default SSH key configured. Add one in Settings → SSH Keys.")
        except Exception as e:
            print(f"  Failed to resolve default key: {e}")
            return TestConnectionResponse(success=False, message=f"Failed to load default SSH key: {str(e)}")

    # Handle saved: key types - fetch from database directly
    if request.ssh_key_type and request.ssh_key_type.startswith('saved:'):
        key_id = request.ssh_key_type.replace('saved:', '')
        print(f"  Resolving saved key: {key_id}")
        try:
            key_uuid = uuid.UUID(key_id)
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ssh_key_encrypted FROM saved_ssh_keys WHERE id = $1", key_uuid
                )
                if row:
                    ssh_key_content = decrypt_credential(row['ssh_key_encrypted'])
                    effective_key_type = 'pasted'
                    print(f"  Resolved saved key, length={len(ssh_key_content)}")
                else:
                    print(f"  Saved key NOT FOUND in database")
                    return TestConnectionResponse(success=False, message="Saved SSH key not found")
        except Exception as e:
            print(f"  Failed to resolve saved key: {e}")
            return TestConnectionResponse(success=False, message=f"Failed to load saved SSH key: {str(e)}")
    
    # If key_type is 'pasted' but no key content, try to load from existing host
    if effective_key_type in ['pasted', 'file'] and not ssh_key_content and request.host_id:
        print(f"  No key content but have host_id={request.host_id}, fetching stored key...")
        try:
            host_uuid = uuid.UUID(request.host_id)
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ssh_private_key_encrypted FROM hosts WHERE id = $1", host_uuid
                )
                if row and row['ssh_private_key_encrypted']:
                    ssh_key_content = decrypt_credential(row['ssh_private_key_encrypted'])
                    effective_key_type = 'pasted'
                    print(f"  Loaded stored key for host, length={len(ssh_key_content)}")
                else:
                    print(f"  No stored key found for host")
        except Exception as e:
            print(f"  Failed to load host key: {e}")
    
    # If key_type is 'pasted' but no key content, it means the frontend didn't send it
    if effective_key_type in ['pasted', 'file'] and not ssh_key_content:
        print(f"  ERROR: key_type={effective_key_type} but no key content!")
        return TestConnectionResponse(
            success=False,
            message="No SSH key content received. Please select or paste a key."
        )
    
    try:
        # Build ssh command
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-p", str(request.ssh_port),
        ]
        
        # Handle authentication
        if effective_key_type in ["pasted", "file"] and ssh_key_content:
            # Write key to temp file
            tmp_key_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            # Normalize line endings (browser textareas may submit CRLF) then
            # ensure trailing newline — OpenSSH requires it or throws "error in libcrypto"
            key_data = ssh_key_content.replace('\r\n', '\n').replace('\r', '\n')
            if not key_data.endswith('\n'):
                key_data += '\n'
            tmp_key_file.write(key_data)
            tmp_key_file.close()
            os.chmod(tmp_key_file.name, 0o600)
            ssh_cmd.extend(["-i", tmp_key_file.name])
            print(f"  Using key file: {tmp_key_file.name} ({len(key_data)} bytes written)")
        elif effective_key_type == "password" and request.ssh_password:
            return await _test_connection_password(request)
        else:
            print(f"  WARNING: No auth method resolved! effective_key_type={effective_key_type}")
        
        # Add user@host and command
        # Use a cross-platform command: 'hostname' works on Linux, macOS, and Windows.
        ssh_cmd.append(f"{request.ssh_user}@{request.hostname}")
        ssh_cmd.append("hostname")
        
        print(f"  Running: ssh -p {request.ssh_port} -i <keyfile> {request.ssh_user}@{request.hostname} hostname")
        
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=15
        )
        
        print(f"  SSH returncode={result.returncode}")
        print(f"  SSH stdout={result.stdout.strip()[:200]}")
        print(f"  SSH stderr={result.stderr.strip()[:200]}")
        
        if result.returncode == 0:
            system_info = result.stdout.strip()

            # Quick OS detection: run 'echo $env:OS' which PowerShell expands
            # to 'Windows_NT' on Windows; bash prints the literal string.
            detected_os = None
            try:
                os_cmd = ssh_cmd[:-1] + ["echo $env:OS"]
                os_result = subprocess.run(os_cmd, capture_output=True, text=True, timeout=10)
                if os_result.returncode == 0:
                    os_out = os_result.stdout.strip()
                    print(f"  OS detect: '{os_out}'")
                    if "Windows_NT" in os_out:
                        detected_os = "Windows"
            except Exception as e:
                print(f"  OS detect failed: {e}")

            return TestConnectionResponse(
                success=True,
                message=f"Successfully connected to {request.hostname}",
                details={
                    "hostname": request.hostname,
                    "port": request.ssh_port,
                    "user": request.ssh_user,
                    "system_info": system_info,
                    "detected_os": detected_os
                }
            )
        else:
            error_msg = result.stderr.strip()
            if "Permission denied" in error_msg:
                msg = "Authentication failed - check username or SSH key"
            elif "Connection refused" in error_msg:
                msg = f"Connection refused on port {request.ssh_port}"
            elif "Connection timed out" in error_msg or "timed out" in error_msg:
                msg = f"Connection timeout - host may be unreachable on port {request.ssh_port}"
            elif "No route to host" in error_msg:
                msg = "No route to host - check network connectivity"
            else:
                msg = f"SSH failed: {error_msg[:200]}"
            
            return TestConnectionResponse(success=False, message=msg)
            
    except subprocess.TimeoutExpired:
        return TestConnectionResponse(success=False, message="Connection timeout after 15s")
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        return TestConnectionResponse(success=False, message=f"Connection test failed: {str(e)}")
    finally:
        if tmp_key_file:
            try:
                os.unlink(tmp_key_file.name)
            except:
                pass


async def _test_connection_password(request: TestConnectionRequest) -> TestConnectionResponse:
    """Fallback for password auth using paramiko (ssh requires sshpass)"""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=request.hostname,
            port=request.ssh_port,
            username=request.ssh_user,
            password=request.ssh_password,
            timeout=10,
        )
        stdin, stdout, stderr = client.exec_command("hostname")
        system_info = stdout.read().decode().strip()
        client.close()
        
        return TestConnectionResponse(
            success=True,
            message=f"Successfully connected to {request.hostname}",
            details={
                "hostname": request.hostname,
                "port": request.ssh_port,
                "user": request.ssh_user,
                "system_info": system_info
            }
        )
    except Exception as e:
        return TestConnectionResponse(
            success=False,
            message=f"Connection failed: {str(e)}"
        )


# ============================================================================
# Bulk Operations
# ============================================================================

@router.post("/hosts/import")
async def import_hosts(request: BulkImportRequest, pool: asyncpg.Pool = Depends(get_db_pool),
                       user: dict = Depends(require_write)):
    """
    Bulk import hosts from various formats.
    
    Supported formats:
    - csv: CSV with columns: hostname,ssh_user,ssh_port,tags,notes
    - json: JSON array of host objects
    """
    import json as json_lib
    import csv
    import io
    
    added = 0
    updated = 0
    failed = 0
    errors = []
    
    hosts_to_import = []
    
    if request.format == "json":
        try:
            hosts_to_import = json_lib.loads(request.data)
            if not isinstance(hosts_to_import, list):
                raise HTTPException(status_code=400, detail="JSON must be an array of host objects")
        except json_lib.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    elif request.format == "csv":
        try:
            import re as _re
            raw = request.data.strip()
            lines = [l for l in raw.splitlines() if l.strip()]
            if not lines:
                raise HTTPException(status_code=400, detail="CSV data is empty")

            # Auto-detect header: first row has text keywords that are column names
            first_fields = [f.strip().lower() for f in lines[0].split(",")]
            has_header = any(f in ("hostname", "ip", "ip_address", "host", "ssh_user", "user") for f in first_fields)

            if has_header:
                reader = csv.DictReader(io.StringIO(raw))
                for row in reader:
                    hn = (row.get("hostname") or row.get("host") or "").strip()
                    ip = (row.get("ip_address") or row.get("ip") or "").strip()
                    if not hn:
                        hn = ip  # fallback: use IP as hostname
                    hosts_to_import.append({
                        "hostname": hn,
                        "ip_address": ip or None,
                        "ssh_user": (row.get("ssh_user") or row.get("user") or "root").strip() or "root",
                        "ssh_port": int(row.get("ssh_port") or row.get("port") or 22),
                        "tags": row.get("tags", "").strip(),
                        "notes": row.get("notes", "").strip(),
                        "is_control_node": str(row.get("is_control_node", "false")).lower() == "true",
                        "allow_auto_reboot": str(row.get("allow_auto_reboot", "true")).lower() != "false",
                    })
            else:
                # Headerless format: HOSTNAME, SSH_USER, SSH_PORT, TAGS, NOTES
                # The hostname can be a DNS name OR an IP address — column 1 is ALWAYS the hostname.
                # If the hostname looks like an IP, also populate ip_address with that same value.
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if not parts or not parts[0]:
                        continue
                    hn = parts[0]
                    user_val = parts[1] if len(parts) > 1 and parts[1] else "root"
                    port_str = parts[2] if len(parts) > 2 and parts[2] else "22"
                    tags_val = parts[3] if len(parts) > 3 else ""
                    notes_val = parts[4] if len(parts) > 4 else ""
                    # If hostname looks like an IP, store it as ip_address too
                    ip_val = hn if _re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', hn) else ""
                    hosts_to_import.append({
                        "hostname": hn,
                        "ip_address": ip_val or None,
                        "ssh_user": user_val or "root",
                        "ssh_port": int(port_str) if port_str.isdigit() else 22,
                        "tags": tags_val,
                        "notes": notes_val,
                        "is_control_node": False,
                        "allow_auto_reboot": True,
                    })
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid CSV: {str(e)}")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {request.format}. Use 'json' or 'csv'.")
    
    # Deduplicate within this batch - last entry for a hostname wins
    seen_in_batch = {}
    for host_data in hosts_to_import:
        hn = host_data.get("hostname", "").strip()
        if hn:
            seen_in_batch[hn] = host_data
    hosts_to_import = list(seen_in_batch.values())

    async with pool.acquire() as conn:
        for host_data in hosts_to_import:
            hostname = host_data.get("hostname", "").strip()
            if not hostname:
                failed += 1
                errors.append("Empty hostname")
                continue
            
            try:
                existing = await conn.fetchval("SELECT id FROM hosts WHERE hostname = $1", hostname)
                
                if existing and not request.overwrite:
                    failed += 1
                    errors.append(f"{hostname}: already exists (overwrite=false)")
                    continue
                
                ssh_user = host_data.get("ssh_user", "root")
                ssh_port = int(host_data.get("ssh_port", 22))
                tags = host_data.get("tags", "")
                notes = host_data.get("notes", "")
                is_control = host_data.get("is_control_node", False)
                allow_reboot = host_data.get("allow_auto_reboot", True)
                ip_address = host_data.get("ip_address", "") or None
                
                if existing:
                    await conn.execute("""
                        UPDATE hosts SET ssh_user=$2, ssh_port=$3, tags=$4, notes=$5,
                               is_control_node=$6, allow_auto_reboot=$7,
                               ip_address=COALESCE($8, ip_address), updated_at=NOW()
                        WHERE hostname=$1
                    """, hostname, ssh_user, ssh_port, tags, notes, is_control, allow_reboot, ip_address)
                    updated += 1
                else:
                    await conn.execute("""
                        INSERT INTO hosts (hostname, ip_address, ssh_user, ssh_port, tags, notes, 
                                          is_control_node, allow_auto_reboot, ssh_key_type, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'default', $9)
                    """, hostname, ip_address, ssh_user, ssh_port, tags, notes, is_control, allow_reboot, user['id'])
                    added += 1
                    
            except Exception as e:
                failed += 1
                errors.append(f"{hostname}: {str(e)}")
    
    # Auto-apply default SSH key to newly added hosts.
    # saved_ssh_keys.ssh_key_encrypted may be TEXT (base64 str) or BYTEA (raw bytes)
    # depending on the live DB column type.  hosts.ssh_private_key_encrypted is BYTEA,
    # so we normalise to bytes before the UPDATE regardless of source type.
    import base64 as _b64
    keys_applied = 0
    if added > 0:
        try:
            async with pool.acquire() as conn:
                default_key_row = await conn.fetchrow(
                    "SELECT id, ssh_key_encrypted FROM saved_ssh_keys WHERE is_default = TRUE LIMIT 1"
                )
                if default_key_row:
                    raw_val = default_key_row['ssh_key_encrypted']
                    if isinstance(raw_val, str):
                        # TEXT column stores base64 — decode to raw Fernet bytes for BYTEA target
                        raw_key_bytes = _b64.b64decode(raw_val.encode('utf-8'))
                    elif isinstance(raw_val, memoryview):
                        raw_key_bytes = bytes(raw_val)
                    else:
                        raw_key_bytes = raw_val  # already bytes

                    imported_hostnames = [h.get("hostname", "").strip() for h in hosts_to_import]
                    for hn in imported_hostnames:
                        if not hn:
                            continue
                        row = await conn.fetchrow(
                            "SELECT id, ssh_key_type FROM hosts WHERE hostname = $1", hn
                        )
                        if row and row['ssh_key_type'] == 'default':
                            await conn.execute("""
                                UPDATE hosts
                                SET ssh_key_type = 'pasted',
                                    ssh_private_key_encrypted = $1,
                                    updated_at = NOW()
                                WHERE id = $2
                            """, raw_key_bytes, row['id'])
                            keys_applied += 1
        except Exception as _e:
            logger.warning(f"Auto-apply default SSH key during import failed (non-fatal): {_e}")

    # Sync inventory after import
    try:
        inventory_path = os.getenv("ANSIBLE_INVENTORY_PATH", "/ansible/hosts")
        await sync_ansible_inventory(pool, inventory_path)
    except Exception:
        pass

    # Trigger Ansible check for newly added hosts
    if added > 0:
        try:
            new_hostnames = [h.get("hostname", "").strip() for h in hosts_to_import if h.get("hostname")]
            if new_hostnames:
                import asyncio
                from app import run_ansible_check_task
                asyncio.create_task(run_ansible_check_task(new_hostnames))
        except Exception:
            pass

    return {
        "added": added,
        "updated": updated,
        "failed": failed,
        "total": len(hosts_to_import),
        "errors": errors[:20],  # Limit error messages
        "ssh_keys_applied": keys_applied
    }



# ============================================================================
# Application Settings
# ============================================================================

@router.get("/app")
async def get_app_settings(pool: asyncpg.Pool = Depends(get_db_pool),
                           user: dict = Depends(require_full_admin)):
    """Get application-wide settings"""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value, description FROM settings ORDER BY key")
        return {row['key']: {"value": row['value'], "description": row['description']} for row in rows}


class SettingUpdate(BaseModel):
    value: str


@router.put("/app/{key}")
async def update_app_setting(key: str, body: SettingUpdate, pool: asyncpg.Pool = Depends(get_db_pool),
                             user: dict = Depends(require_full_admin)):
    """Update an application setting"""
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES ($1, $2, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = CURRENT_TIMESTAMP
        """, key, body.value)
        
        await log_audit_action(pool, "UPDATE", "setting", key, {"value": body.value})
        
        return {"key": key, "value": body.value}


# ============================================================================
# Ansible Inventory Sync
# ============================================================================

@router.post("/sync-inventory")
async def sync_inventory(pool: asyncpg.Pool = Depends(get_db_pool),
                         user: dict = Depends(require_full_admin)):
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
async def get_system_info(user: dict = Depends(require_full_admin)):
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
            "database_connected": True,  # If we got here, DB is connected
            "install_mode": os.getenv("PATCHPILOT_INSTALL_MODE", "docker").lower(),
        }
    except subprocess.TimeoutExpired:
        return {
            "ansible_version": "Detection timeout",
            "python_version": f"Python {sys.version.split()[0]}",
            "encryption_enabled": bool(os.getenv("PATCHPILOT_ENCRYPTION_KEY")),
            "database_connected": True,
            "install_mode": os.getenv("PATCHPILOT_INSTALL_MODE", "docker").lower(),
        }
    except Exception as e:
        logger.error(f"Failed to get system info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get system info: {str(e)}")

# ============================================================================
# Saved SSH Keys Endpoints
# ============================================================================

@router.get("/ssh-keys", response_model=List[SavedSSHKeyResponse])
async def list_saved_ssh_keys(pool: asyncpg.Pool = Depends(get_db_pool),
                              user: dict = Depends(require_auth)):
    """List saved SSH keys, scoped by role."""
    uid = owner_id(user)
    async with pool.acquire() as conn:
        if uid is not None:
            rows = await conn.fetch("""
                SELECT k.id, k.name, k.is_default, k.created_at, k.updated_at,
                       u.username AS owner_username
                FROM saved_ssh_keys k
                LEFT JOIN users u ON k.created_by = u.id
                WHERE k.created_by = $1
                ORDER BY k.is_default DESC, k.name ASC
            """, uid)
        else:
            rows = await conn.fetch("""
                SELECT k.id, k.name, k.is_default, k.created_at, k.updated_at,
                       u.username AS owner_username
                FROM saved_ssh_keys k
                LEFT JOIN users u ON k.created_by = u.id
                ORDER BY k.is_default DESC, k.name ASC
            """)
        return [dict(row) for row in rows]

@router.get("/ssh-keys/{key_id}", response_model=SavedSSHKeyResponse)
async def get_saved_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                            user: dict = Depends(require_auth)):
    """Get a specific saved SSH key (ownership-scoped)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        if not await verify_ssh_key_ownership(conn, user, key_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this SSH key")
        row = await conn.fetchrow("""
            SELECT id, name, is_default, created_at, updated_at
            FROM saved_ssh_keys
            WHERE id = $1
        """, key_uuid)
        
        if not row:
            raise HTTPException(status_code=404, detail=f"SSH key with ID {key_id} not found")
        
        return dict(row)

@router.post("/ssh-keys", response_model=SavedSSHKeyResponse, status_code=201)
async def create_saved_ssh_key(key: SavedSSHKeyCreate, pool: asyncpg.Pool = Depends(get_db_pool),
                               user: dict = Depends(require_write)):
    """Create a new saved SSH key (write-only, sets created_by)"""
    # Check if name already exists
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM saved_ssh_keys WHERE name = $1",
            key.name
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"SSH key '{key.name}' already exists")
        
        # Encrypt the SSH key — encode to UTF-8 bytes for BYTEA column
        try:
            ssh_key_encrypted = encrypt_credential(key.ssh_key).encode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encrypt SSH key: {e}")
            raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        # If this should be default, unset other defaults FOR THIS USER ONLY
        if key.is_default:
            await conn.execute(
                "UPDATE saved_ssh_keys SET is_default = FALSE WHERE created_by = $1",
                user['id']
            )
        
        # Insert the key
        row = await conn.fetchrow("""
            INSERT INTO saved_ssh_keys (name, ssh_key_encrypted, is_default, created_by)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, is_default, created_at, updated_at
        """, key.name, ssh_key_encrypted, key.is_default, user['id'])
        
        logger.info(f"Created saved SSH key: {key.name}")
        return dict(row)

@router.put("/ssh-keys/{key_id}", response_model=SavedSSHKeyResponse)
async def update_saved_ssh_key(
    key_id: str,
    key: SavedSSHKeyUpdate,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user: dict = Depends(require_write)
):
    """Update a saved SSH key (ownership-scoped, write-only)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        if not await verify_ssh_key_ownership(conn, user, key_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this SSH key")
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
                ssh_key_encrypted = encrypt_credential(key.ssh_key).encode('utf-8')
                updates.append(f"ssh_key_encrypted = ${param_count}")
                values.append(ssh_key_encrypted)
                param_count += 1
            except Exception as e:
                logger.error(f"Failed to encrypt SSH key: {e}")
                raise HTTPException(status_code=500, detail="Failed to encrypt SSH key")
        
        if key.is_default is not None:
            if key.is_default:
                # Unset other defaults FOR THIS USER ONLY
                await conn.execute(
                    "UPDATE saved_ssh_keys SET is_default = FALSE WHERE created_by = $1 AND id != $2",
                    user['id'], key_uuid
                )
            
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
async def delete_saved_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                               user: dict = Depends(require_write)):
    """Delete a saved SSH key (ownership-scoped, write-only)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        if not await verify_ssh_key_ownership(conn, user, key_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this SSH key")
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
async def get_decrypted_ssh_key(key_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                                user: dict = Depends(require_auth)):
    """Get the decrypted SSH key content (ownership-scoped)"""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID format")
    
    async with pool.acquire() as conn:
        if not await verify_ssh_key_ownership(conn, user, key_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this SSH key")
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
