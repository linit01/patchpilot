"""
PatchPilot — License & Trial Management

Trial logic:
  - 14-day trial starts when first-run setup is completed
  - Trial status is stored in the settings table (trial_started_at)
  - Once expired, API endpoints return 403 and the UI shows an expiry screen
  - A license key can be entered to activate (full validation in v0.12)

Settings keys used:
  - trial_started_at   ISO timestamp of when trial began
  - license_key        User-entered license key (validated in v0.12)
  - license_status     'trial' | 'trial_expired' | 'active' | 'suspended'
"""
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["license"])

# ── Configuration ─────────────────────────────────────────────────────────────
TRIAL_DAYS = int(os.getenv("PATCHPILOT_TRIAL_DAYS", "14"))

# ── Database pool (injected at startup) ───────────────────────────────────────
_pool = None


def license_set_pool(pool):
    """Called from app.py startup to inject the DB pool."""
    global _pool
    _pool = pool


async def _get_pool():
    if _pool is None:
        raise HTTPException(status_code=503, detail="Database not ready")
    return _pool


# ── Settings helpers ──────────────────────────────────────────────────────────
async def _get_setting(pool, key: str) -> Optional[str]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT value FROM settings WHERE key = $1", key
        )


async def _set_setting(pool, key: str, value: str, description: str = ""):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, description, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
        """, key, value, description)


# ── Trial management ──────────────────────────────────────────────────────────
async def start_trial(pool):
    """
    Called when first-run setup completes. Records the trial start time.
    Safe to call multiple times — only sets if not already set.
    """
    existing = await _get_setting(pool, "trial_started_at")
    if existing:
        logger.info(f"Trial already started at {existing} — not resetting")
        return

    now = datetime.now(timezone.utc).isoformat()
    await _set_setting(pool, "trial_started_at", now, "UTC timestamp when trial began")
    await _set_setting(pool, "license_status", "trial", "Current license status")
    logger.info(f"Trial started: {now} ({TRIAL_DAYS} days)")


async def get_license_status(pool) -> dict:
    """
    Returns the current license/trial status.

    Returns:
        {
            "status": "trial" | "trial_expired" | "active" | "no_setup",
            "trial_days_total": 14,
            "trial_days_remaining": int or None,
            "trial_expires_at": ISO string or None,
            "license_key_set": bool,
        }
    """
    license_key = await _get_setting(pool, "license_key")
    license_status = await _get_setting(pool, "license_status")
    trial_started = await _get_setting(pool, "trial_started_at")

    # If a license key is set and status is active, they're licensed
    # (Full validation happens in v0.12 — for now, any key = active)
    if license_key and license_status == "active":
        return {
            "status": "active",
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": None,
            "trial_expires_at": None,
            "license_key_set": True,
        }

    # No trial started yet — setup hasn't been completed
    if not trial_started:
        return {
            "status": "no_setup",
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": TRIAL_DAYS,
            "trial_expires_at": None,
            "license_key_set": bool(license_key),
        }

    # Calculate trial remaining
    try:
        started = datetime.fromisoformat(trial_started)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        expires = started + timedelta(days=TRIAL_DAYS)
        now = datetime.now(timezone.utc)
        remaining = (expires - now).total_seconds()
        days_remaining = max(0, int(remaining / 86400))
    except Exception as e:
        logger.error(f"Error parsing trial_started_at: {e}")
        days_remaining = 0
        expires = datetime.now(timezone.utc)

    if days_remaining <= 0:
        # Update status in DB if not already expired
        if license_status != "trial_expired":
            await _set_setting(pool, "license_status", "trial_expired",
                               "Current license status")
        return {
            "status": "trial_expired",
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": 0,
            "trial_expires_at": expires.isoformat(),
            "license_key_set": bool(license_key),
        }

    return {
        "status": "trial",
        "trial_days_total": TRIAL_DAYS,
        "trial_days_remaining": days_remaining,
        "trial_expires_at": expires.isoformat(),
        "license_key_set": bool(license_key),
    }


# ── Middleware helper ─────────────────────────────────────────────────────────
async def check_trial_active(pool) -> bool:
    """
    Returns True if the app is licensed or within the trial period.
    Used by API endpoints to gate access when trial expires.
    """
    status = await get_license_status(pool)
    return status["status"] in ("trial", "active", "no_setup")


async def require_license(pool) -> bool:
    """
    Returns True only if a valid license is active (not trial).
    Used to gate features that are disabled during trial:
      - Backup create/restore/upload/download/delete
    """
    status = await get_license_status(pool)
    return status["status"] == "active"


async def enforce_license(pool):
    """
    Raises HTTP 403 if no active license. Call at the top of gated endpoints.
    """
    if not await require_license(pool):
        raise HTTPException(
            status_code=403,
            detail="This feature requires an active PatchPilot license. "
                   "Visit https://getpatchpilot.app to purchase."
        )


async def enforce_trial_active(pool):
    """
    Raises HTTP 403 if trial has expired and no license is active.
    Call at the top of general API endpoints after trial expires.
    """
    if not await check_trial_active(pool):
        raise HTTPException(
            status_code=403,
            detail="Your PatchPilot trial has expired. "
                   "Enter a license key or visit https://getpatchpilot.app to continue."
        )


# ── API Endpoints ─────────────────────────────────────────────────────────────
class LicenseKeyRequest(BaseModel):
    license_key: str = Field(..., min_length=1, max_length=512)


@router.get("/status")
async def license_status_endpoint():
    """Return current license/trial status."""
    pool = await _get_pool()
    status = await get_license_status(pool)
    return status


@router.post("/activate")
async def activate_license(req: LicenseKeyRequest):
    """
    Store a license key and mark as active.
    In v0.12 this will validate the key against the license server.
    For now, any non-empty key activates the license.
    """
    pool = await _get_pool()

    # Store the key
    await _set_setting(pool, "license_key", req.license_key,
                       "PatchPilot license key")
    await _set_setting(pool, "license_status", "active",
                       "Current license status")

    logger.info("License key activated (v0.11 — key stored, full validation in v0.12)")

    return {
        "status": "active",
        "message": "License activated successfully.",
    }


@router.post("/deactivate")
async def deactivate_license():
    """Remove the license key and revert to trial status."""
    pool = await _get_pool()

    trial_started = await _get_setting(pool, "trial_started_at")

    # Clear key
    await _set_setting(pool, "license_key", "", "PatchPilot license key")

    # Determine if trial is still valid
    if trial_started:
        status = await get_license_status(pool)
        if status["trial_days_remaining"] and status["trial_days_remaining"] > 0:
            await _set_setting(pool, "license_status", "trial",
                               "Current license status")
            return {"status": "trial", "message": "License removed. Trial still active."}
        else:
            await _set_setting(pool, "license_status", "trial_expired",
                               "Current license status")
            return {"status": "trial_expired", "message": "License removed. Trial has expired."}

    return {"status": "no_setup", "message": "License removed."}
