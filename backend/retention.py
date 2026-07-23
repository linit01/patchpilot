"""patch_history / audit_log retention — bound Postgres growth.

Runs from a daily periodic loop (launched in app.py startup) and on demand via
POST /api/settings/retention/run. Windows are admin-tunable knobs in the
`settings` table (no restart needed):

  * patch_history_retention_days — delete patch runs older than this (default 30)
  * audit_log_retention_days     — delete audit entries older (default 90; 0 = keep forever)

patch_history also stores raw ansible stdout ONLY on failure now (app.py); this
prune bounds the row count on top of that per-row size fix.
"""

import logging

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_PATCH_HISTORY_RETENTION_DAYS = 30
DEFAULT_AUDIT_LOG_RETENTION_DAYS = 90


def _rowcount(status: str) -> int:
    """asyncpg execute() returns e.g. 'DELETE 42' — pull the count."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def _get_int_setting(conn: asyncpg.Connection, key: str, default: int) -> int:
    row = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
    if row is None or str(row).strip() == "":
        return default
    try:
        return int(row)
    except (TypeError, ValueError):
        return default


async def run_retention_once(pool: asyncpg.Pool) -> dict:
    """Delete patch_history / audit_log rows past their window. A window of
    0 (or negative) disables that table's prune (keep forever). Idempotent."""
    async with pool.acquire() as conn:
        patch_days = await _get_int_setting(
            conn, "patch_history_retention_days", DEFAULT_PATCH_HISTORY_RETENTION_DAYS
        )
        audit_days = await _get_int_setting(
            conn, "audit_log_retention_days", DEFAULT_AUDIT_LOG_RETENTION_DAYS
        )

        patch_deleted = 0
        if patch_days > 0:
            patch_deleted = _rowcount(
                await conn.execute(
                    "DELETE FROM patch_history "
                    "WHERE execution_time < NOW() - make_interval(days => $1)",
                    patch_days,
                )
            )

        audit_deleted = 0
        if audit_days > 0:
            audit_deleted = _rowcount(
                await conn.execute(
                    "DELETE FROM audit_log "
                    "WHERE timestamp < NOW() - make_interval(days => $1)",
                    audit_days,
                )
            )

    if patch_deleted or audit_deleted:
        logger.info(
            "retention: deleted %d patch_history + %d audit_log row(s)",
            patch_deleted, audit_deleted,
        )
    return {
        "patch_history_deleted": patch_deleted,
        "audit_log_deleted": audit_deleted,
        "patch_history_retention_days": patch_days,
        "audit_log_retention_days": audit_days,
    }


async def retention_stats(pool: asyncpg.Pool) -> dict:
    """Row counts + on-disk table sizes — for the settings/admin visibility
    surface so growth is visible before the 5Gi Postgres PVC fills."""
    async with pool.acquire() as conn:
        ph = await conn.fetchrow(
            "SELECT count(*) AS n, "
            "pg_total_relation_size('patch_history') AS b FROM patch_history"
        )
        al = await conn.fetchrow(
            "SELECT count(*) AS n, "
            "pg_total_relation_size('audit_log') AS b FROM audit_log"
        )
    return {
        "patch_history_rows": int(ph["n"]),
        "patch_history_bytes": int(ph["b"] or 0),
        "audit_log_rows": int(al["n"]),
        "audit_log_bytes": int(al["b"] or 0),
    }
