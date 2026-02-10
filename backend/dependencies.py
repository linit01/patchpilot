"""
Database dependency for Settings API
Provides the asyncpg pool from app.state
"""
import asyncpg
import os
from fastapi import Request

# Global pool variable
_pool: asyncpg.Pool = None

async def create_pool():
    """Create database connection pool"""
    global _pool
    print("Creating database pool for Settings API...")
    _pool = await asyncpg.create_pool(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "patchpilot"),
        password=os.getenv("POSTGRES_PASSWORD", "patchpilot"),
        database=os.getenv("POSTGRES_DB", "patchpilot"),
        min_size=2,
        max_size=10
    )
    print("Database pool created for Settings API")
    return _pool

async def close_pool():
    """Close database connection pool"""
    global _pool
    if _pool:
        await _pool.close()
        print("Settings API pool closed")

async def get_db_pool(request: Request = None) -> asyncpg.Pool:
    """
    FastAPI dependency to get database pool
    Usage in route:
        @router.get("/endpoint")
        async def my_endpoint(pool: asyncpg.Pool = Depends(get_db_pool)):
            async with pool.acquire() as conn:
                ...
    """
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call create_pool() first.")
    return _pool
