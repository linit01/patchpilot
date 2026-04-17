import logging

logger = logging.getLogger(__name__)
import asyncpg
import os
from typing import Optional, List, Dict

class DatabaseClient:
    def __init__(self):
        # Store connection params individually — never build a URL string with the
        # password embedded in it. Passwords containing URL special chars (@, :, /)
        # silently corrupt the parsed host/port and cause cryptic connection errors.
        self._db_host     = os.getenv('POSTGRES_HOST', 'localhost')
        self._db_port     = int(os.getenv('POSTGRES_PORT', '5432'))
        self._db_user     = os.getenv('POSTGRES_USER', 'patchpilot')
        self._db_password = os.getenv('POSTGRES_PASSWORD', 'patchpilot')
        self._db_name     = os.getenv('POSTGRES_DB', 'patchpilot')
        self.pool = None

    async def connect(self):
        """Create database connection pool"""
        if not self.pool:
            self.pool = await asyncpg.create_pool(
                host=self._db_host,
                port=self._db_port,
                user=self._db_user,
                password=self._db_password,
                database=self._db_name,
            )
    
    async def close(self):
        """Close database connection pool"""
        if self.pool:
            await self.pool.close()
            print("DatabaseClient connection closed")

    async def execute(self, query: str, *args):
        """Execute a query"""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)
    
    async def fetch(self, query: str, *args):
        """Fetch multiple rows"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)
    
    async def fetchrow(self, query: str, *args):
        """Fetch a single row"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def get_all_hosts(self) -> List[Dict]:
        """Get all hosts from database"""
        query = "SELECT * FROM hosts ORDER BY hostname"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]
    
    async def get_host_by_hostname(self, hostname: str) -> Optional[Dict]:
        """Get a specific host by hostname"""
        query = "SELECT * FROM hosts WHERE hostname = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, hostname)
            return dict(row) if row else None

    async def upsert_host(self, hostname: str, ip_address: str, os_type: str,
                         os_family: str, status: str, total_updates: int,
                         reboot_required: bool = False) -> Optional[Dict]:
        """Insert or update a host"""
        query = """
            INSERT INTO hosts (hostname, ip_address, os_type, os_family, status, total_updates, reboot_required, last_checked)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (hostname)
            DO UPDATE SET
                ip_address = CASE WHEN EXCLUDED.ip_address != '' THEN EXCLUDED.ip_address ELSE hosts.ip_address END,
                os_type = CASE WHEN EXCLUDED.os_type != '' THEN EXCLUDED.os_type ELSE hosts.os_type END,
                os_family = CASE WHEN EXCLUDED.os_family != '' THEN EXCLUDED.os_family ELSE hosts.os_family END,
                status = EXCLUDED.status,
                total_updates = EXCLUDED.total_updates,
                reboot_required = EXCLUDED.reboot_required,
                last_checked = NOW()
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, hostname, ip_address, os_type, os_family, status, total_updates, reboot_required)
            return dict(row) if row else None

    async def get_packages_for_host(self, host_id: str) -> List[Dict]:
        """Get all packages for a specific host"""
        query = "SELECT * FROM packages WHERE host_id = $1 ORDER BY package_name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, host_id)
            return [dict(row) for row in rows]

    async def delete_packages_for_host(self, host_id: str):
        """Delete all packages for a host"""
        query = "DELETE FROM packages WHERE host_id = $1"
        async with self.pool.acquire() as conn:
            await conn.execute(query, host_id)

    async def upsert_package(self, host_id: str, package_name: str,
                            current_version: str, available_version: str,
                            update_type: str = "apt",
                            package_id: str = None) -> Optional[Dict]:
        """Insert or update a package. package_id stores MAS numeric ID or winget Package.Id."""
        query = """
            INSERT INTO packages (host_id, package_name, current_version, available_version, update_type, package_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (host_id, package_name)
            DO UPDATE SET
                current_version = EXCLUDED.current_version,
                available_version = EXCLUDED.available_version,
                update_type = EXCLUDED.update_type,
                package_id = EXCLUDED.package_id
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, host_id, package_name, current_version, available_version, update_type, package_id)
            return dict(row) if row else None

    async def insert_package(self, host_id: str, package_name: str,
                            current_version: str, available_version: str):
        """Insert a package (legacy method for compatibility)"""
        return await self.upsert_package(host_id, package_name, current_version, available_version)

    async def record_patch_execution(self, host_id: str, status: str, 
                                    packages_updated: int, execution_time: float,
                                    output: str = "") -> Optional[Dict]:
        """Record a patch execution in history"""
        query = """
            INSERT INTO patch_history (host_id, status, packages_updated, execution_time, output)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, host_id, status, packages_updated, execution_time, output)
            return dict(row) if row else None

    async def get_patch_history(self, host_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get patch history, optionally filtered by host"""
        if host_id:
            query = "SELECT * FROM patch_history WHERE host_id = $1 ORDER BY execution_time DESC LIMIT $2"
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, host_id, limit)
                return [dict(row) for row in rows]
        else:
            query = "SELECT * FROM patch_history ORDER BY execution_time DESC LIMIT $1"
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, limit)
                return [dict(row) for row in rows]

    async def get_stats(self):
        """Get summary statistics"""
        query = """
            SELECT
                COUNT(*) as total_hosts,
                COUNT(*) FILTER (WHERE status = 'up-to-date') as up_to_date,
                COUNT(*) FILTER (WHERE status = 'updates-available') as need_updates,
                COUNT(*) FILTER (WHERE status = 'unreachable') as unreachable,
                COALESCE(SUM(total_updates), 0) as total_pending_updates
            FROM hosts
        """
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query)
            return {
                "total_hosts": result['total_hosts'],
                "up_to_date": result['up_to_date'],
                "need_updates": result['need_updates'],
                "unreachable": result['unreachable'],
                "total_pending_updates": result['total_pending_updates']
            }
