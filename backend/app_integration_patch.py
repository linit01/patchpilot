"""
app.py — Integration patch for backup/restore feature
=======================================================
Add these snippets to your existing app.py.

Search for the relevant section in your app.py and add the lines marked with ✚.
All changes are additive — nothing existing needs to be removed.
"""

# ─── SECTION 1: Imports (add near the top with your other imports) ───────────

from backup_restore import router as backup_router, set_pool, maintenance_mode  # ✚

# ─── SECTION 2: Router registration (add after app = FastAPI(...)) ────────────

app.include_router(backup_router)  # ✚

# ─── SECTION 3: Pool sharing (add inside your startup event / lifespan) ──────
# Find your existing startup event that creates the asyncpg pool and add:

@app.on_event("startup")
async def startup():
    # ... your existing pool creation code ...
    # pool = await asyncpg.create_pool(DATABASE_URL, ...)

    set_pool(pool)  # ✚  Share pool reference with backup module

# ─── SECTION 4: Maintenance mode guard (add to any write endpoints) ──────────
# For extra safety, guard patching/scheduling endpoints:

from fastapi import Request  # already imported in most apps
from backup_restore import maintenance_mode, maintenance_reason  # ✚

@app.middleware("http")  # ✚
async def maintenance_gate(request: Request, call_next):  # ✚
    """Block mutating requests during backup/restore operations."""  # ✚
    WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}  # ✚
    # Allow backup/status endpoint through so UI can poll progress  # ✚
    is_backup_route = request.url.path.startswith("/api/backup")  # ✚
    if maintenance_mode and request.method in WRITE_METHODS and not is_backup_route:  # ✚
        from fastapi.responses import JSONResponse  # ✚
        return JSONResponse(  # ✚
            status_code=503,  # ✚
            content={  # ✚
                "detail": f"System in maintenance mode: {maintenance_reason}",  # ✚
                "maintenance": True,  # ✚
            }  # ✚
        )  # ✚
    return await call_next(request)  # ✚
