"""
PatchPilot - Auto-Patch Scheduling API
Handles creation, management, and execution of scheduled patch windows.
All hosts in a schedule share the same SUDO password.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, List
import asyncpg
import uuid
import logging
from datetime import time as dt_time, datetime

from dependencies import get_db_pool
from auth import require_auth, require_write
from encryption_utils import encrypt_credential, decrypt_credential
from rbac import owner_id, owner_id_or_param, verify_schedule_ownership, verify_host_ownership

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/schedules", tags=["schedules"], dependencies=[Depends(require_auth)])


# ============================================================================
# Pydantic Models
# ============================================================================

class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    enabled: bool = True
    day_of_week: str = Field(default="sunday", description="Comma-separated days: monday,wednesday,friday")
    start_time: str = Field(default="02:00", description="HH:MM format")
    end_time: str = Field(default="04:00", description="HH:MM format")
    auto_reboot: bool = False
    become_password: Optional[str] = Field(None, description="Shared SUDO password for all hosts in this schedule")
    host_ids: List[str] = Field(default=[], description="List of host UUIDs to include")


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    day_of_week: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    auto_reboot: Optional[bool] = None
    become_password: Optional[str] = None
    host_ids: Optional[List[str]] = None


class ScheduleResponse(BaseModel):
    id: str
    name: str
    enabled: bool
    day_of_week: str
    start_time: str
    end_time: str
    auto_reboot: bool
    has_password: bool
    host_count: int
    hosts: List[dict]
    last_run: Optional[str]
    last_status: Optional[str]
    created_at: str
    updated_at: str


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/active")
async def get_active_schedules(owner: str = None,
                               pool: asyncpg.Pool = Depends(get_db_pool),
                               user: dict = Depends(require_auth)):
    """Return schedules that are running, recently completed, or have pending retries.
    Lightweight endpoint polled by the dashboard activity bar every 60 seconds.
    Schedules stuck in 'running' for > 45 minutes are auto-corrected to 'error' in the DB
    so the frontend pill always clears eventually even if the backend crashed mid-run."""
    from datetime import datetime, timezone, timedelta
    uid = owner_id_or_param(user, owner)
    async with pool.acquire() as conn:
        # Auto-correct stale 'running' rows so they don't stick forever
        await conn.execute("""
            UPDATE patch_schedules
            SET last_status = 'error'
            WHERE last_status = 'running'
              AND last_run < NOW() - INTERVAL '45 minutes'
        """)

        base_query = """
            SELECT id, name, last_status, last_run,
                   array_length(retry_host_ids, 1) AS retry_count
            FROM patch_schedules
            WHERE enabled = TRUE
              AND (
                  last_status IN ('running', 'partial')
                  OR (last_status IN ('success', 'error')
                      AND last_run > NOW() - INTERVAL '30 minutes')
              )
        """
        if uid is not None:
            base_query += " AND created_by = $1"
            base_query += " ORDER BY last_run DESC NULLS LAST"
            rows = await conn.fetch(base_query, uid)
        else:
            base_query += " ORDER BY last_run DESC NULLS LAST"
            rows = await conn.fetch(base_query)
        return [
            {
                "id": str(r['id']),
                "name": r['name'],
                "last_status": r['last_status'],
                "last_run": r['last_run'].isoformat() if r['last_run'] else None,
                "retry_count": r['retry_count'] or 0,
            }
            for r in rows
        ]


@router.get("")
async def list_schedules(owner: str = None,
                         pool: asyncpg.Pool = Depends(get_db_pool),
                         user: dict = Depends(require_auth)):
    """List patch schedules, scoped by role."""
    uid = owner_id_or_param(user, owner)
    async with pool.acquire() as conn:
        if uid is not None:
            schedules = await conn.fetch("""
                SELECT s.*,
                       COUNT(sh.host_id) as host_count
                FROM patch_schedules s
                LEFT JOIN patch_schedule_hosts sh ON s.id = sh.schedule_id
                WHERE s.created_by = $1
                GROUP BY s.id
                ORDER BY s.name
            """, uid)
        else:
            schedules = await conn.fetch("""
                SELECT s.*,
                       COUNT(sh.host_id) as host_count
                FROM patch_schedules s
                LEFT JOIN patch_schedule_hosts sh ON s.id = sh.schedule_id
                GROUP BY s.id
                ORDER BY s.name
            """)
        
        result = []
        for sched in schedules:
            # Get hosts for this schedule
            hosts = await conn.fetch("""
                SELECT h.id, h.hostname, h.status, h.os_family
                FROM hosts h
                JOIN patch_schedule_hosts sh ON h.id = sh.host_id
                WHERE sh.schedule_id = $1
                ORDER BY h.hostname
            """, sched['id'])
            
            result.append({
                "id": str(sched['id']),
                "name": sched['name'],
                "enabled": sched['enabled'],
                "day_of_week": sched['day_of_week'],
                "start_time": str(sched['start_time'])[:5],
                "end_time": str(sched['end_time'])[:5],
                "auto_reboot": sched['auto_reboot'],
                "has_password": sched['become_password_encrypted'] is not None,
                "host_count": sched['host_count'],
                "hosts": [{"id": str(h['id']), "hostname": h['hostname'], 
                          "status": h['status'], "os_family": h['os_family']} for h in hosts],
                "last_run": sched['last_run'].isoformat() if sched['last_run'] else None,
                "last_status": sched['last_status'],
                "retry_count": len(sched['retry_host_ids']) if dict(sched).get('retry_host_ids') else 0,
                "created_at": sched['created_at'].isoformat(),
                "updated_at": sched['updated_at'].isoformat()
            })
        
        return result


@router.post("", status_code=201)
async def create_schedule(schedule: ScheduleCreate, pool: asyncpg.Pool = Depends(get_db_pool),
                          user: dict = Depends(require_write)):
    """Create a new auto-patch schedule (write-only, sets created_by)"""
    # Validate time format
    try:
        start = dt_time.fromisoformat(schedule.start_time)
        end = dt_time.fromisoformat(schedule.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM")
    
    # Validate days
    valid_days = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
    days = [d.strip().lower() for d in schedule.day_of_week.split(',')]
    for d in days:
        if d not in valid_days:
            raise HTTPException(status_code=400, detail=f"Invalid day: {d}")
    
    # Encrypt password if provided
    password_encrypted = None
    if schedule.become_password:
        password_encrypted = encrypt_credential(schedule.become_password)
    
    async with pool.acquire() as conn:
        # Create schedule
        row = await conn.fetchrow("""
            INSERT INTO patch_schedules (name, enabled, day_of_week, start_time, end_time, 
                                         auto_reboot, become_password_encrypted, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """, schedule.name, schedule.enabled, ','.join(days),
            start, end, schedule.auto_reboot, password_encrypted, user['id'])
        
        schedule_id = row['id']
        
        # Add hosts
        for host_id_str in schedule.host_ids:
            try:
                host_uuid = uuid.UUID(host_id_str)
                # Verify host exists
                exists = await conn.fetchval("SELECT id FROM hosts WHERE id = $1", host_uuid)
                if exists:
                    await conn.execute("""
                        INSERT INTO patch_schedule_hosts (schedule_id, host_id) 
                        VALUES ($1, $2) ON CONFLICT DO NOTHING
                    """, schedule_id, host_uuid)
            except ValueError:
                continue
        
        logger.info(f"Created schedule '{schedule.name}' with {len(schedule.host_ids)} hosts")
        return {"id": str(schedule_id), "message": f"Schedule '{schedule.name}' created"}


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                       user: dict = Depends(require_auth)):
    """Get a specific schedule with its hosts (ownership-scoped)"""
    try:
        sched_uuid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid schedule ID")
    
    async with pool.acquire() as conn:
        if not await verify_schedule_ownership(conn, user, sched_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this schedule")
        sched = await conn.fetchrow("SELECT * FROM patch_schedules WHERE id = $1", sched_uuid)
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        hosts = await conn.fetch("""
            SELECT h.id, h.hostname, h.status, h.os_family
            FROM hosts h
            JOIN patch_schedule_hosts sh ON h.id = sh.host_id
            WHERE sh.schedule_id = $1
        """, sched_uuid)
        
        return {
            "id": str(sched['id']),
            "name": sched['name'],
            "enabled": sched['enabled'],
            "day_of_week": sched['day_of_week'],
            "start_time": str(sched['start_time'])[:5],
            "end_time": str(sched['end_time'])[:5],
            "auto_reboot": sched['auto_reboot'],
            "has_password": sched['become_password_encrypted'] is not None,
            "host_count": len(hosts),
            "hosts": [{"id": str(h['id']), "hostname": h['hostname'],
                      "status": h['status'], "os_family": h['os_family']} for h in hosts],
            "last_run": sched['last_run'].isoformat() if sched['last_run'] else None,
            "last_status": sched['last_status'],
            "created_at": sched['created_at'].isoformat(),
            "updated_at": sched['updated_at'].isoformat()
        }


@router.put("/{schedule_id}")
async def update_schedule(schedule_id: str, schedule: ScheduleUpdate, 
                          pool: asyncpg.Pool = Depends(get_db_pool),
                          user: dict = Depends(require_write)):
    """Update an existing schedule (ownership-scoped, write-only)"""
    try:
        sched_uuid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid schedule ID")
    
    async with pool.acquire() as conn:
        if not await verify_schedule_ownership(conn, user, sched_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this schedule")
        existing = await conn.fetchrow("SELECT * FROM patch_schedules WHERE id = $1", sched_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        updates = []
        values = []
        idx = 1
        
        if schedule.name is not None:
            updates.append(f"name = ${idx}"); values.append(schedule.name); idx += 1
        if schedule.enabled is not None:
            updates.append(f"enabled = ${idx}"); values.append(schedule.enabled); idx += 1
        if schedule.day_of_week is not None:
            days = [d.strip().lower() for d in schedule.day_of_week.split(',')]
            updates.append(f"day_of_week = ${idx}"); values.append(','.join(days)); idx += 1
        if schedule.start_time is not None:
            updates.append(f"start_time = ${idx}"); values.append(dt_time.fromisoformat(schedule.start_time)); idx += 1
        if schedule.end_time is not None:
            updates.append(f"end_time = ${idx}"); values.append(dt_time.fromisoformat(schedule.end_time)); idx += 1
        if schedule.auto_reboot is not None:
            updates.append(f"auto_reboot = ${idx}"); values.append(schedule.auto_reboot); idx += 1
        if schedule.become_password is not None:
            encrypted = encrypt_credential(schedule.become_password) if schedule.become_password else None
            updates.append(f"become_password_encrypted = ${idx}"); values.append(encrypted); idx += 1
        
        if updates:
            updates.append(f"updated_at = NOW()")
            values.append(sched_uuid)
            await conn.execute(
                f"UPDATE patch_schedules SET {', '.join(updates)} WHERE id = ${idx}",
                *values
            )
        
        # Update hosts if provided
        if schedule.host_ids is not None:
            await conn.execute("DELETE FROM patch_schedule_hosts WHERE schedule_id = $1", sched_uuid)
            for host_id_str in schedule.host_ids:
                try:
                    host_uuid = uuid.UUID(host_id_str)
                    exists = await conn.fetchval("SELECT id FROM hosts WHERE id = $1", host_uuid)
                    if exists:
                        await conn.execute("""
                            INSERT INTO patch_schedule_hosts (schedule_id, host_id) 
                            VALUES ($1, $2) ON CONFLICT DO NOTHING
                        """, sched_uuid, host_uuid)
                except ValueError:
                    continue
        
        return {"message": f"Schedule updated"}


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                          user: dict = Depends(require_write)):
    """Delete a schedule (ownership-scoped, write-only)"""
    try:
        sched_uuid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid schedule ID")
    
    async with pool.acquire() as conn:
        if not await verify_schedule_ownership(conn, user, sched_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this schedule")
        result = await conn.fetchrow(
            "DELETE FROM patch_schedules WHERE id = $1 RETURNING name", sched_uuid
        )
        if not result:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        logger.info(f"Deleted schedule: {result['name']}")


@router.post("/{schedule_id}/run")
async def trigger_schedule(schedule_id: str, pool: asyncpg.Pool = Depends(get_db_pool),
                           user: dict = Depends(require_write)):
    """Manually trigger a schedule to run now (ownership-scoped, write-only)"""
    try:
        sched_uuid = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid schedule ID")
    
    async with pool.acquire() as conn:
        if not await verify_schedule_ownership(conn, user, sched_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this schedule")
        sched = await conn.fetchrow("SELECT * FROM patch_schedules WHERE id = $1", sched_uuid)
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        # Get hosts
        host_rows = await conn.fetch("""
            SELECT h.hostname FROM hosts h
            JOIN patch_schedule_hosts sh ON h.id = sh.host_id
            WHERE sh.schedule_id = $1
        """, sched_uuid)
        
        hostnames = [r['hostname'] for r in host_rows]
        if not hostnames:
            raise HTTPException(status_code=400, detail="No hosts in this schedule")
        
        # Decrypt password
        become_password = None
        if sched['become_password_encrypted']:
            become_password = decrypt_credential(sched['become_password_encrypted'])
        
        # Mark as running
        await conn.execute("""
            UPDATE patch_schedules SET last_run = NOW(), last_status = 'running'
            WHERE id = $1
        """, sched_uuid)
    
    # Import and launch
    import asyncio
    from app import run_scheduled_patch
    asyncio.create_task(run_scheduled_patch(sched_uuid, hostnames, become_password, pool))
    
    return {"message": f"Schedule '{sched['name']}' triggered for {len(hostnames)} host(s)"}
