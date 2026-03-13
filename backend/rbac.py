"""
PatchPilot - Role-Based Access Control Helpers

Centralises ownership filtering and permission checks so individual
endpoints don't need to duplicate the logic.

Roles:
    full_admin  — sees/manages everything; exactly one per install
    admin       — CRUD on own resources only (hosts, keys, schedules)
    viewer      — read-only view of everything
"""

import uuid
import logging
from typing import Optional
import asyncpg

logger = logging.getLogger(__name__)


# ── Ownership filter ────────────────────────────────────────────────────────

def owner_id(user: dict) -> Optional[uuid.UUID]:
    """Return the user's ID if scoping applies (admin), else None (full_admin/viewer)."""
    if user['role'] == 'admin':
        return user['id']
    return None


def owner_id_or_param(user: dict, query_owner: Optional[str] = None) -> Optional[uuid.UUID]:
    """For full_admin with ?owner= filter dropdown; admin always scoped to self.

    Returns:
        UUID to filter by, or None for 'show all'.
    """
    if user['role'] == 'admin':
        return user['id']
    if user['role'] == 'full_admin' and query_owner:
        try:
            return uuid.UUID(query_owner)
        except ValueError:
            pass
    return None


# ── SQL clause builders ─────────────────────────────────────────────────────

def hosts_where(user: dict, query_owner: Optional[str] = None,
                table_alias: str = "", param_offset: int = 1) -> tuple:
    """Build a WHERE clause fragment + params for host ownership scoping.

    Returns (clause_str, params_list, next_param_offset).
    clause_str is empty string if no filter needed (full_admin / viewer with no owner param).
    """
    uid = owner_id_or_param(user, query_owner)
    prefix = f"{table_alias}." if table_alias else ""
    if uid is not None:
        return f" AND {prefix}created_by = ${param_offset}", [uid], param_offset + 1
    return "", [], param_offset


async def verify_host_ownership(conn: asyncpg.Connection, user: dict,
                                host_id: uuid.UUID) -> bool:
    """Return True if the user is allowed to access this host.
    full_admin → always True.  admin → only if created_by matches.  viewer → True (read-only enforced elsewhere).
    """
    if user['role'] in ('full_admin', 'viewer'):
        return True
    created_by = await conn.fetchval(
        "SELECT created_by FROM hosts WHERE id = $1", host_id
    )
    return created_by == user['id']


async def verify_host_ownership_by_hostname(conn: asyncpg.Connection, user: dict,
                                            hostname: str) -> bool:
    """Same as verify_host_ownership but by hostname string."""
    if user['role'] in ('full_admin', 'viewer'):
        return True
    created_by = await conn.fetchval(
        "SELECT created_by FROM hosts WHERE hostname = $1", hostname
    )
    return created_by == user['id']


async def verify_schedule_ownership(conn: asyncpg.Connection, user: dict,
                                    schedule_id: uuid.UUID) -> bool:
    """Return True if the user is allowed to access this schedule."""
    if user['role'] in ('full_admin', 'viewer'):
        return True
    created_by = await conn.fetchval(
        "SELECT created_by FROM patch_schedules WHERE id = $1", schedule_id
    )
    return created_by == user['id']


async def verify_ssh_key_ownership(conn: asyncpg.Connection, user: dict,
                                   key_id: uuid.UUID) -> bool:
    """Return True if the user is allowed to access this SSH key."""
    if user['role'] in ('full_admin', 'viewer'):
        return True
    created_by = await conn.fetchval(
        "SELECT created_by FROM saved_ssh_keys WHERE id = $1", key_id
    )
    return created_by == user['id']


# ── Write guard ─────────────────────────────────────────────────────────────

def can_write(user: dict) -> bool:
    """Viewers cannot write."""
    return user['role'] != 'viewer'
