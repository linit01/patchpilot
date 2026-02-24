"""
PatchPilot - Ansible Inventory Sync
Syncs hosts from PostgreSQL database to Ansible inventory file
"""

import asyncpg
import logging
import os
from typing import List, Dict

logger = logging.getLogger(__name__)

DEFAULT_SSH_USER = os.getenv("DEFAULT_SSH_USER", "root")


async def sync_ansible_inventory(pool: asyncpg.Pool, inventory_path: str = "/ansible/hosts"):
    """
    Sync all hosts from database to Ansible inventory file.

    Args:
        pool: Database connection pool
        inventory_path: Path to Ansible hosts file

    Returns:
        int: Number of hosts synced
    """
    async with pool.acquire() as conn:
        hosts = await conn.fetch("""
            SELECT hostname, ssh_user, ssh_port, is_control_node
            FROM hosts
            ORDER BY hostname
        """)

    if not hosts:
        logger.warning("No hosts found in database to sync")
        return 0

    # Build Ansible inventory
    inventory_lines = ["[homelab]"]

    for host in hosts:
        line = host['hostname']

        if host.get('is_control_node'):
            line += " ansible_connection=local"
        else:
            # Only emit ansible_user if it differs from the install-time default
            if host['ssh_user'] and host['ssh_user'] != DEFAULT_SSH_USER:
                line += f" ansible_user={host['ssh_user']}"

        if host['ssh_port'] and host['ssh_port'] != 22:
            line += f" ansible_port={host['ssh_port']}"

        inventory_lines.append(line)

    inventory_lines.append("")
    inventory_lines.append("[homelab:vars]")
    inventory_lines.append(f"ansible_user={DEFAULT_SSH_USER}")

    # Write to file — log on failure, do NOT re-raise (non-fatal)
    try:
        with open(inventory_path, 'w') as f:
            f.write('\n'.join(inventory_lines) + '\n')
        logger.info(f"✓ Synced {len(hosts)} hosts to Ansible inventory: {inventory_path}")
        return len(hosts)
    except Exception as e:
        logger.error(f"Failed to write Ansible inventory at {inventory_path}: {e}")
        return 0


async def add_host_to_inventory(pool: asyncpg.Pool, hostname: str, ssh_user: str = None, 
                                ssh_port: int = 22, inventory_path: str = "/ansible/hosts"):
    """
    Quick add: Append a single host to inventory without full resync.
    Falls back to full sync if file format is unexpected.
    
    Args:
        pool: Database connection pool
        hostname: Hostname to add
        ssh_user: SSH username
        ssh_port: SSH port
        inventory_path: Path to Ansible hosts file
    """
    # For safety, always do full sync to maintain consistency
    return await sync_ansible_inventory(pool, inventory_path)


async def remove_host_from_inventory(pool: asyncpg.Pool, hostname: str, 
                                     inventory_path: str = "/ansible/hosts"):
    """
    Remove a host from inventory.
    Does full resync to maintain consistency.
    
    Args:
        pool: Database connection pool
        hostname: Hostname to remove
        inventory_path: Path to Ansible hosts file
    """
    return await sync_ansible_inventory(pool, inventory_path)


if __name__ == "__main__":
    # Test sync function
    import asyncio
    import os
    
    async def test_sync():
        # Create connection pool
        pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "patchpilot"),
            password=os.getenv("POSTGRES_PASSWORD", "patchpilot"),
            database=os.getenv("POSTGRES_DB", "patchpilot")
        )
        
        # Test sync
        count = await sync_ansible_inventory(pool, "/tmp/test_hosts")
        print(f"✓ Synced {count} hosts")
        
        # Show result
        with open("/tmp/test_hosts", 'r') as f:
            print("\nGenerated inventory:")
            print(f.read())
        
        await pool.close()
    
    asyncio.run(test_sync())
