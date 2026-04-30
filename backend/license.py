"""
PatchPilot — License & Trial Management

Trial logic:
  - 14-day trial starts when first-run setup is completed
  - Trial status is stored in the settings table (trial_started_at)
  - Once expired, API endpoints return 403 and the UI shows an expiry screen

License logic (provider-agnostic — see backend/license_providers/):
  - User enters license key from purchase
  - Backend asks the active provider to activate(key, install_uuid)
  - Provider returns a normalized instance_id — stored alongside the key
  - Periodic validation (every 7 days) calls provider.validate(...)
  - 30-day grace period if the license server is unreachable
  - Subscription expiry/cancellation detected via validation response

Settings keys used (kept stable across providers — values map 1:1):
  - trial_started_at       ISO timestamp of when trial began
  - license_key            License key (provider-issued, opaque to PP)
  - license_instance_id    Provider's activation instance ID
  - license_status         'trial' | 'trial_expired' | 'active' | 'expired' | 'disabled'
  - license_last_validated ISO timestamp of last successful validation
  - license_customer_name  Customer name from license server (for display)
  - license_customer_email Customer email from license server (for display)
  - install_uuid           Unique installation identifier (generated once)
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from license_providers import get_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["license"])

# ── Configuration ─────────────────────────────────────────────────────────────
TRIAL_DAYS = int(os.getenv("PATCHPILOT_TRIAL_DAYS", "14"))
GRACE_DAYS = int(os.getenv("PATCHPILOT_GRACE_DAYS", "30"))
VALIDATION_INTERVAL_HOURS = int(os.getenv("PATCHPILOT_LICENSE_CHECK_HOURS", "168"))  # 7 days

PURCHASE_URL = "https://getpatchpilot.app/buy"

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
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT value FROM settings WHERE key = $1", key
            )
    except Exception:
        return None


async def _set_setting(pool, key: str, value: str, description: str = ""):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, description, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
        """, key, value, description)


# ── Install UUID ──────────────────────────────────────────────────────────────
async def _get_or_create_install_uuid(pool) -> str:
    """
    Returns a persistent UUID for this installation.
    Generated once, stored in settings, survives restarts.
    Passed to the license provider as the activation identifier.
    """
    existing = await _get_setting(pool, "install_uuid")
    if existing:
        return existing
    new_uuid = str(uuid.uuid4())
    await _set_setting(pool, "install_uuid", new_uuid,
                       "Unique installation identifier for license activation")
    logger.info(f"Generated install UUID: {new_uuid}")
    return new_uuid


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

    # Generate install UUID early
    await _get_or_create_install_uuid(pool)

    logger.info(f"Trial started: {now} ({TRIAL_DAYS} days)")


async def ensure_trial_for_existing_installs(pool):
    """
    Called at startup. Handles upgrades from pre-license versions:
    If users exist (setup is complete) but trial_started_at is not set,
    start the trial now. This covers restores and upgrades from older versions.
    """
    trial_started = await _get_setting(pool, "trial_started_at")
    if trial_started:
        return  # Already has trial data — nothing to do

    # Check if setup is complete (users exist)
    try:
        async with pool.acquire() as conn:
            user_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if user_count and user_count > 0:
            logger.info("[License] Existing install detected without trial data — starting trial")
            await start_trial(pool)
    except Exception as e:
        logger.warning(f"[License] Could not check for existing install: {e}")


# ── License status ────────────────────────────────────────────────────────────
async def get_license_status(pool) -> dict:
    """Returns the current license/trial status."""
    license_key = await _get_setting(pool, "license_key")
    license_status = await _get_setting(pool, "license_status")
    instance_id = await _get_setting(pool, "license_instance_id")
    last_validated = await _get_setting(pool, "license_last_validated")
    trial_started = await _get_setting(pool, "trial_started_at")
    customer_name = await _get_setting(pool, "license_customer_name")
    customer_email = await _get_setting(pool, "license_customer_email")

    # Active license — check grace period
    if license_key and instance_id and license_status == "active":
        grace_ok = True
        if last_validated:
            try:
                lv = datetime.fromisoformat(last_validated)
                if lv.tzinfo is None:
                    lv = lv.replace(tzinfo=timezone.utc)
                grace_expires = lv + timedelta(days=GRACE_DAYS)
                if datetime.now(timezone.utc) > grace_expires:
                    grace_ok = False
                    await _set_setting(pool, "license_status", "expired",
                                       "Current license status")
                    license_status = "expired"
            except Exception:
                pass

        if grace_ok:
            return {
                "status": "active",
                "trial_days_total": TRIAL_DAYS,
                "trial_days_remaining": None,
                "trial_expires_at": None,
                "license_key_set": True,
                "customer_name": customer_name or "",
                "customer_email": customer_email or "",
            }

    # Expired or disabled license
    if license_status in ("expired", "disabled"):
        return {
            "status": license_status,
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": 0,
            "trial_expires_at": None,
            "license_key_set": bool(license_key),
            "customer_name": customer_name or "",
            "customer_email": customer_email or "",
        }

    # No trial started yet
    if not trial_started:
        return {
            "status": "no_setup",
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": TRIAL_DAYS,
            "trial_expires_at": None,
            "license_key_set": bool(license_key),
            "customer_name": "",
            "customer_email": "",
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
        if license_status != "trial_expired":
            await _set_setting(pool, "license_status", "trial_expired",
                               "Current license status")
        return {
            "status": "trial_expired",
            "trial_days_total": TRIAL_DAYS,
            "trial_days_remaining": 0,
            "trial_expires_at": expires.isoformat(),
            "license_key_set": bool(license_key),
            "customer_name": "",
            "customer_email": "",
        }

    return {
        "status": "trial",
        "trial_days_total": TRIAL_DAYS,
        "trial_days_remaining": days_remaining,
        "trial_expires_at": expires.isoformat(),
        "license_key_set": bool(license_key),
        "customer_name": "",
        "customer_email": "",
    }


# ── Middleware helpers ─────────────────────────────────────────────────────────
async def check_trial_active(pool) -> bool:
    """Returns True if the app is licensed or within the trial period."""
    status = await get_license_status(pool)
    return status["status"] in ("trial", "active", "no_setup")


async def require_license(pool) -> bool:
    """Returns True only if a valid license is active (not trial)."""
    status = await get_license_status(pool)
    return status["status"] == "active"


async def enforce_license(pool):
    """Raises HTTP 403 if no active license."""
    if not await require_license(pool):
        raise HTTPException(
            status_code=403,
            detail="This feature requires an active PatchPilot license. "
                   f"Visit {PURCHASE_URL} to purchase."
        )


async def enforce_trial_active(pool):
    """Raises HTTP 403 if trial has expired and no license is active."""
    if not await check_trial_active(pool):
        raise HTTPException(
            status_code=403,
            detail="Your PatchPilot trial has expired. "
                   f"Enter a license key or visit {PURCHASE_URL} to continue."
        )


# ── Periodic validation background task ───────────────────────────────────────
async def periodic_license_check():
    """
    Background task: validates the license key with the active provider every
    VALIDATION_INTERVAL_HOURS. Launched from app.py startup.
    """
    await asyncio.sleep(60)

    while True:
        try:
            if _pool is None:
                await asyncio.sleep(300)
                continue

            license_key = await _get_setting(_pool, "license_key")
            instance_id = await _get_setting(_pool, "license_instance_id")
            license_status = await _get_setting(_pool, "license_status")

            if not license_key or not instance_id or license_status != "active":
                await asyncio.sleep(3600)
                continue

            logger.info("[License] Periodic validation check...")

            try:
                result = await get_provider().validate(license_key, instance_id)

                if result.ok:
                    now = datetime.now(timezone.utc).isoformat()
                    await _set_setting(_pool, "license_last_validated", now,
                                       "Last successful license validation")

                    if result.status == "expired":
                        await _set_setting(_pool, "license_status", "expired",
                                           "Current license status")
                        logger.warning("[License] Subscription expired per license server")
                    elif result.status == "disabled":
                        await _set_setting(_pool, "license_status", "disabled",
                                           "Current license status")
                        logger.warning("[License] License disabled by admin")
                    else:
                        logger.info(f"[License] Validation successful, status: {result.status}")
                else:
                    # Authoritative reject from the license server (4xx with
                    # an explicit error) — flip license_status so the UI and
                    # subsequent /api/license/status calls reflect reality.
                    # Transient failures (network, 5xx) raise and land in the
                    # except block below, where the 30-day grace period applies.
                    await _set_setting(_pool, "license_status", "expired",
                                       "Current license status")
                    logger.warning(
                        f"[License] Authoritative validation failure — "
                        f"flipped status to expired: {result.error or 'unknown error'}"
                    )

            except Exception as e:
                logger.warning(f"[License] Could not reach license server: {e}")

        except Exception as e:
            logger.error(f"[License] Periodic check error: {e}", exc_info=True)

        await asyncio.sleep(VALIDATION_INTERVAL_HOURS * 3600)


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
    Activate a license key with the active provider.
    On success, stores the key, instance_id, and customer info.
    """
    pool = await _get_pool()
    install_uuid = await _get_or_create_install_uuid(pool)

    try:
        result = await get_provider().activate(req.license_key, install_uuid)
    except Exception as e:
        logger.error(f"License activation error: {e}")
        raise HTTPException(
            status_code=502,
            detail="Could not reach the license server. Check your internet connection and try again."
        )

    if not result.ok:
        logger.warning(f"License activation failed: {result.error}")
        if result.activation_limit_reached:
            raise HTTPException(
                status_code=400,
                detail="This license key has reached its activation limit. "
                       "Deactivate it on your other installation first, or "
                       f"contact support at {PURCHASE_URL}"
            )
        raise HTTPException(status_code=400, detail=result.error)

    now = datetime.now(timezone.utc).isoformat()
    await _set_setting(pool, "license_key", req.license_key,
                       "PatchPilot license key")
    await _set_setting(pool, "license_instance_id", result.instance_id,
                       "License server activation instance ID")
    await _set_setting(pool, "license_status", "active",
                       "Current license status")
    await _set_setting(pool, "license_last_validated", now,
                       "Last successful license validation")
    await _set_setting(pool, "license_customer_name", result.customer_name,
                       "Customer name from license server")
    await _set_setting(pool, "license_customer_email", result.customer_email,
                       "Customer email from license server")

    logger.info(f"License activated: instance={result.instance_id}, "
                f"customer={result.customer_name or 'unknown'}")

    return {
        "status": "active",
        "message": "License activated successfully.",
        "customer_name": result.customer_name,
    }


@router.post("/deactivate")
async def deactivate_license():
    """
    Deactivate the license key with the active provider and revert to trial.
    Frees the activation slot so the key can be used on another machine.
    """
    pool = await _get_pool()

    license_key = await _get_setting(pool, "license_key")
    instance_id = await _get_setting(pool, "license_instance_id")

    if not license_key or not instance_id:
        raise HTTPException(status_code=400, detail="No active license to deactivate.")

    try:
        result = await get_provider().deactivate(license_key, instance_id)
        if not result.ok:
            logger.warning(f"License deactivation warning: {result.error}")
    except Exception as e:
        logger.warning(f"Could not reach license server for deactivation: {e}")

    await _set_setting(pool, "license_key", "", "PatchPilot license key")
    await _set_setting(pool, "license_instance_id", "",
                       "License server activation instance ID")
    await _set_setting(pool, "license_last_validated", "",
                       "Last successful license validation")
    await _set_setting(pool, "license_customer_name", "",
                       "Customer name from license server")
    await _set_setting(pool, "license_customer_email", "",
                       "Customer email from license server")

    trial_started = await _get_setting(pool, "trial_started_at")
    if trial_started:
        status = await get_license_status(pool)
        if status.get("trial_days_remaining", 0) > 0:
            await _set_setting(pool, "license_status", "trial",
                               "Current license status")
            return {"status": "trial",
                    "message": "License deactivated. Trial still active."}
        else:
            await _set_setting(pool, "license_status", "trial_expired",
                               "Current license status")
            return {"status": "trial_expired",
                    "message": "License deactivated. Trial has expired."}

    await _set_setting(pool, "license_status", "trial",
                       "Current license status")
    return {"status": "trial", "message": "License deactivated."}


@router.post("/validate")
async def validate_license_now():
    """Manually trigger a license validation check."""
    pool = await _get_pool()

    license_key = await _get_setting(pool, "license_key")
    instance_id = await _get_setting(pool, "license_instance_id")

    if not license_key or not instance_id:
        raise HTTPException(status_code=400, detail="No active license to validate.")

    try:
        result = await get_provider().validate(license_key, instance_id)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the license server: {str(e)}"
        )

    if not result.ok:
        # Authoritative reject from the license server (4xx with an explicit
        # error) — flip license_status so subsequent /api/license/status calls
        # reflect reality. Transient failures (network, 5xx) raise and are
        # caught above as a 502; the grace period handles those without
        # touching the stored status.
        await _set_setting(pool, "license_status", "expired",
                           "Current license status")
        return {"valid": False, "status": "invalid",
                "message": result.error or "Validation failed."}

    now = datetime.now(timezone.utc).isoformat()
    await _set_setting(pool, "license_last_validated", now,
                       "Last successful license validation")

    if result.status in ("expired", "disabled"):
        await _set_setting(pool, "license_status", result.status,
                           "Current license status")
        return {"valid": False, "status": result.status,
                "message": f"License is {result.status}."}

    return {"valid": True, "status": "active",
            "message": "License is valid and active."}
