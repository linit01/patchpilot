"""
PatchPilot - Authentication API
Handles user login, logout, session management, and route protection.

Option C: Read-only dashboard is public, all actions require authentication.
"""

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field
from typing import Optional
import asyncpg
import bcrypt
import secrets
import uuid
import logging
from datetime import datetime, timedelta, timezone

from dependencies import get_db_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["authentication"])

# Session duration: 24 hours
SESSION_DURATION_HOURS = 24
SESSION_COOKIE_NAME = "patchpilot_session"


# ==========================================================================
# Pydantic Models
# ==========================================================================

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=255)


class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    last_login: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=255)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=8, max_length=255)
    role: str = Field(default="viewer", pattern="^(admin|viewer)$")


# ==========================================================================
# Helper Functions
# ==========================================================================

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))


def generate_session_token() -> str:
    """Generate a cryptographically secure session token"""
    return secrets.token_urlsafe(48)


async def get_current_user(request: Request, pool: asyncpg.Pool = Depends(get_db_pool)) -> Optional[dict]:
    """
    Extract and validate session from cookie.
    Returns the user dict if authenticated, None otherwise.
    """
    # Check Bearer token first (mobile app), then fall back to cookie (web)
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.username, u.email, u.role, u.is_active
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.token = $1
              AND s.expires_at > NOW()
              AND u.is_active = true
        """, token)

    if not row:
        return None

    return dict(row)


async def require_auth(request: Request, pool: asyncpg.Pool = Depends(get_db_pool)) -> dict:
    """
    Dependency that requires authentication.
    Raises 401 if not authenticated.
    """
    user = await get_current_user(request, pool)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def require_admin(request: Request, pool: asyncpg.Pool = Depends(get_db_pool)) -> dict:
    """
    Dependency that requires admin-level role (full_admin OR admin).
    Raises 401/403 if not authenticated or insufficient role.
    """
    user = await require_auth(request, pool)
    if user['role'] not in ('full_admin', 'admin'):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_full_admin(request: Request, pool: asyncpg.Pool = Depends(get_db_pool)) -> dict:
    """
    Dependency that requires the full_admin role (app owner).
    Used for: Settings, Backup/Restore, Uninstall, User Management, Audit Log.
    """
    user = await require_auth(request, pool)
    if user['role'] != 'full_admin':
        raise HTTPException(status_code=403, detail="Full admin access required")
    return user


async def require_write(request: Request, pool: asyncpg.Pool = Depends(get_db_pool)) -> dict:
    """
    Dependency that requires write access (full_admin or admin, NOT viewer).
    Viewers get 403 on any mutating endpoint.
    """
    user = await require_auth(request, pool)
    if user['role'] == 'viewer':
        raise HTTPException(status_code=403, detail="Read-only access — viewers cannot modify resources")
    return user


def ownership_filter(user: dict) -> Optional[uuid.UUID]:
    """
    Returns the user's UUID if they should only see their own resources (admin role),
    or None if they can see everything (full_admin, viewer).
    """
    if user['role'] == 'admin':
        return user['id']
    return None


async def log_audit(pool: asyncpg.Pool, user_id: Optional[str], username: str,
                    action: str, resource_type: str = None, resource_id: str = None,
                    details: dict = None, ip_address: str = None,
                    user_agent: str = None, success: bool = True):
    """Write an entry to the audit log"""
    try:
        import json
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO audit_log (user_id, username, action, resource_type,
                    resource_id, details, ip_address, user_agent, success)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
            """, user_id, username, action, resource_type, resource_id,
                json.dumps(details) if details else None,
                ip_address, user_agent, success)
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")


# ==========================================================================
# Auth Endpoints
# ==========================================================================

@router.post("/login")
async def login(login_req: LoginRequest, request: Request, response: Response,
                pool: asyncpg.Pool = Depends(get_db_pool)):
    """Authenticate user and create session"""
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username = $1 AND is_active = true",
            login_req.username
        )

    if not user or not verify_password(login_req.password, user['password_hash']):
        # Log failed attempt
        await log_audit(pool, None, login_req.username, "login_failed",
                        ip_address=ip_address, user_agent=user_agent, success=False)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Generate session
    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)

    async with pool.acquire() as conn:
        # Create session
        await conn.execute("""
            INSERT INTO sessions (user_id, token, expires_at, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5)
        """, user['id'], token, expires_at, ip_address, user_agent)

        # Update last_login
        await conn.execute(
            "UPDATE users SET last_login = NOW(), updated_at = NOW() WHERE id = $1",
            user['id']
        )

    # Log successful login
    await log_audit(pool, str(user['id']), user['username'], "login",
                    ip_address=ip_address, user_agent=user_agent)

    # Set session cookie (HTTP-only for security)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_DURATION_HOURS * 3600,
        path="/"
    )

    return {
        "message": "Login successful",
        "token": token,
        "user": {
            "id": str(user['id']),
            "username": user['username'],
            "email": user['email'],
            "role": user['role']
        }
    }


@router.post("/logout")
async def logout(request: Request, response: Response,
                 pool: asyncpg.Pool = Depends(get_db_pool)):
    """Destroy session and clear cookie"""
    # Check Bearer token first (mobile app), then cookie (web)
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.cookies.get(SESSION_COOKIE_NAME)

    if token:
        async with pool.acquire() as conn:
            # Get user info for audit log before deleting session
            session = await conn.fetchrow(
                "SELECT user_id FROM sessions WHERE token = $1", token
            )
            if session:
                user = await conn.fetchrow(
                    "SELECT username FROM users WHERE id = $1", session['user_id']
                )
                if user:
                    await log_audit(pool, str(session['user_id']), user['username'],
                                    "logout", ip_address=request.client.host if request.client else None)

            # Delete session
            await conn.execute("DELETE FROM sessions WHERE token = $1", token)

    # Clear cookie
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"message": "Logged out"}


@router.get("/me")
async def get_current_user_info(request: Request,
                                pool: asyncpg.Pool = Depends(get_db_pool)):
    """Get current authenticated user info (or null if not logged in)"""
    user = await get_current_user(request, pool)
    if not user:
        return {"authenticated": False, "user": None}

    return {
        "authenticated": True,
        "user": {
            "id": str(user['id']),
            "username": user['username'],
            "email": user['email'],
            "role": user['role']
        }
    }


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request,
                          pool: asyncpg.Pool = Depends(get_db_pool),
                          user: dict = Depends(require_auth)):
    """Change password for the authenticated user"""
    async with pool.acquire() as conn:
        db_user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user['id'])

    if not db_user or not verify_password(req.current_password, db_user['password_hash']):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_hash = hash_password(req.new_password)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
            new_hash, user['id']
        )

    await log_audit(pool, str(user['id']), user['username'], "password_changed",
                    ip_address=request.client.host if request.client else None)

    return {"message": "Password changed successfully"}


@router.get("/check-setup")
async def check_setup(pool: asyncpg.Pool = Depends(get_db_pool)):
    """Check if any users exist (for initial setup flow).
    Returns both has_users (legacy) and setup_required (new) for compatibility.
    Handles the race where the endpoint is called before startup migrations finish."""
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
        has_users = count > 0
        return {
            "has_users": has_users,
            "setup_required": not has_users,
            "user_count": count,
        }
    except Exception:
        # Table doesn't exist yet (startup migration still running) — safe default
        return {
            "has_users": False,
            "setup_required": True,
            "user_count": 0,
        }


@router.post("/setup")
async def initial_setup(login_req: LoginRequest, request: Request,
                        response: Response,
                        pool: asyncpg.Pool = Depends(get_db_pool)):
    """
    Create the first admin user. Only works if no users exist yet.
    This replaces the default 'admin/admin' user with a real one.
    """
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE username != 'admin' OR password_hash != '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5eidgvK0K3jq6'")
        if count > 0:
            raise HTTPException(status_code=400, detail="Setup already completed")

        # Delete the default admin user if it exists
        await conn.execute("DELETE FROM users WHERE username = 'admin'")

        # Create the real admin user
        password_hash = hash_password(login_req.password)
        user = await conn.fetchrow("""
            INSERT INTO users (username, email, password_hash, role)
            VALUES ($1, $2, $3, 'full_admin')
            RETURNING *
        """, login_req.username, f"{login_req.username}@patchpilot.local", password_hash)

    # Auto-login after setup
    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions (user_id, token, expires_at, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5)
        """, user['id'], token, expires_at,
            request.client.host if request.client else None,
            request.headers.get("user-agent", ""))

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_DURATION_HOURS * 3600,
        path="/"
    )

    await log_audit(pool, str(user['id']), user['username'], "initial_setup",
                    ip_address=request.client.host if request.client else None)

    return {
        "message": "Admin user created and logged in",
        "user": {
            "id": str(user['id']),
            "username": user['username'],
            "role": "full_admin"
        }
    }


# ==========================================================================
# User Management (admin only)
# ==========================================================================

@router.get("/users")
async def list_users(user: dict = Depends(require_full_admin),
                     pool: asyncpg.Pool = Depends(get_db_pool)):
    """List all users (full_admin only)"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, username, email, role, is_active, created_at, last_login FROM users ORDER BY created_at"
        )
        return [dict(r) for r in rows]


@router.post("/users", status_code=201)
async def create_user(req: CreateUserRequest, request: Request,
                      user: dict = Depends(require_full_admin),
                      pool: asyncpg.Pool = Depends(get_db_pool)):
    """Create a new user (full_admin only). Cannot create another full_admin."""
    password_hash = hash_password(req.password)
    email = f"{req.username}@patchpilot.local"
    
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO users (username, email, password_hash, role)
                   VALUES ($1, $2, $3, $4) RETURNING id, username, email, role, is_active""",
                req.username, email, password_hash, req.role
            )
            await log_audit(pool, user['id'], user['username'], "create_user",
                          "user", str(row['id']), f"Created user: {req.username} ({req.role})",
                          request.client.host if request.client else None)
            return dict(row)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Username '{req.username}' already exists")


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request,
                      user: dict = Depends(require_full_admin),
                      pool: asyncpg.Pool = Depends(get_db_pool)):
    """Delete a user (full_admin only, cannot delete self or another full_admin)"""
    if user_id == str(user['id']):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT username, role FROM users WHERE id = $1", uid)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target['role'] == 'full_admin':
            raise HTTPException(status_code=400, detail="Cannot delete the full admin account")
        
        # Delete their sessions first (CASCADE should handle, but be explicit)
        await conn.execute("DELETE FROM sessions WHERE user_id = $1", uid)
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        
        await log_audit(pool, user['id'], user['username'], "delete_user",
                      "user", user_id, f"Deleted user: {target['username']}",
                      request.client.host if request.client else None)
        
    return {"message": f"User '{target['username']}' deleted"}


# ==========================================================================
# Session Cleanup (call from startup)
# ==========================================================================

async def cleanup_expired_sessions(pool: asyncpg.Pool):
    """Remove expired sessions from the database"""
    try:
        async with pool.acquire() as conn:
            deleted = await conn.execute(
                "DELETE FROM sessions WHERE expires_at < NOW()"
            )
            logger.info(f"Cleaned up expired sessions: {deleted}")
    except Exception as e:
        logger.error(f"Session cleanup failed: {e}")
