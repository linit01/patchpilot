"""
Freemius license provider.

API surface (no-auth path — sandbox vs production is determined by which key
the license was issued under at checkout, not by anything the backend sends):

  POST /v1/products/{product_id}/licenses/activate.json
       body: uid, license_key
       returns: install_id, install_public_key, install_secret_key,
                license_id, plan_id, expiration, user_email, ...

  POST /v1/products/{product_id}/licenses/deactivate.json
       body: uid, license_key

  Validate: re-issue activate.json with the same uid + license_key. Freemius
  treats activate as idempotent for an existing (uid, license_key) pair and
  returns the current install/license state — including expiration and
  cancellation status — without consuming an extra activation slot. If
  sandbox testing reveals this assumption breaks, switch to the
  GET /v1/products/{product_id}/licenses/{license_id}.json path with an
  Authorization: Bearer header.

Configuration:
  PATCHPILOT_FREEMIUS_PRODUCT_ID  required, integer (e.g. 28811)
  PATCHPILOT_FREEMIUS_API_BASE    optional, defaults to https://api.freemius.com/v1
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import ActivateResult, DeactivateResult, ValidateResult

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.freemius.com/v1"


def _api_base() -> str:
    return os.getenv("PATCHPILOT_FREEMIUS_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _product_id() -> str:
    pid = os.getenv("PATCHPILOT_FREEMIUS_PRODUCT_ID", "").strip()
    if not pid:
        raise RuntimeError(
            "PATCHPILOT_FREEMIUS_PRODUCT_ID is not set. "
            "Set it to your Freemius product ID (e.g. 28811)."
        )
    return pid


def _is_license_active(payload: dict[str, Any]) -> str:
    """
    Map the Freemius response to our normalized status:
      "active" | "expired" | "disabled"
    Freemius signals expiration via the `expiration` ISO timestamp and
    cancellation via `is_cancelled`/`is_block_features` flags. We treat
    block_features=true as 'disabled'.
    """
    if payload.get("is_block_features"):
        return "disabled"

    expiration = payload.get("expiration")
    if expiration and expiration not in ("", "0000-00-00 00:00:00"):
        try:
            # Freemius returns "YYYY-MM-DD HH:MM:SS" (UTC, per their docs).
            exp = datetime.strptime(expiration, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if datetime.now(timezone.utc) > exp:
                return "expired"
        except ValueError:
            logger.warning("Freemius: unparseable expiration %r", expiration)

    return "active"


class FreemiusProvider:
    name = "freemius"

    async def activate(self, license_key: str, install_uuid: str) -> ActivateResult:
        url = f"{_api_base()}/products/{_product_id()}/licenses/activate.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"uid": install_uuid, "license_key": license_key},
                headers={"Accept": "application/json"},
            )

        try:
            payload = resp.json()
        except Exception:
            return ActivateResult(
                ok=False,
                error=f"Freemius returned non-JSON response (HTTP {resp.status_code}).",
            )

        if resp.status_code >= 400 or "error" in payload:
            err = payload.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            msg = msg or f"Activation failed (HTTP {resp.status_code})."
            err_type = (err.get("type") or "").lower() if isinstance(err, dict) else ""
            return ActivateResult(
                ok=False,
                error=msg,
                activation_limit_reached="activation" in err_type and "limit" in err_type,
            )

        fs_install_id = str(payload.get("install_id") or payload.get("id") or "")
        if not fs_install_id:
            return ActivateResult(
                ok=False,
                error="Freemius response missing install_id.",
            )

        first = (payload.get("user_first_name") or "").strip()
        last = (payload.get("user_last_name") or "").strip()
        full_name = (f"{first} {last}").strip()

        # Return install_uuid as instance_id so validate/deactivate (which key
        # off uid + license_key) can be called with just (key, instance_id).
        # Log Freemius's own install_id for support traceability.
        logger.info(
            "Freemius activation: fs_install_id=%s, license_id=%s",
            fs_install_id, payload.get("license_id") or payload.get("id"),
        )
        return ActivateResult(
            ok=True,
            instance_id=install_uuid,
            customer_name=full_name,
            customer_email=payload.get("user_email", "") or "",
        )

    async def validate(self, license_key: str, instance_id: str) -> ValidateResult:
        # instance_id == install_uuid (set in activate above). Re-issue
        # activate.json with the same (uid, license_key) pair — Freemius
        # treats this as idempotent and returns the current install/license
        # state without consuming an extra activation slot.
        url = f"{_api_base()}/products/{_product_id()}/licenses/activate.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"uid": instance_id, "license_key": license_key},
                headers={"Accept": "application/json"},
            )

        try:
            payload = resp.json()
        except Exception:
            return ValidateResult(
                ok=False,
                status="invalid",
                error=f"Freemius returned non-JSON response (HTTP {resp.status_code}).",
            )

        if resp.status_code >= 400 or "error" in payload:
            err = payload.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return ValidateResult(
                ok=False, status="invalid", error=msg or "Validation failed."
            )

        return ValidateResult(ok=True, status=_is_license_active(payload))

    async def deactivate(self, license_key: str, instance_id: str) -> DeactivateResult:
        url = f"{_api_base()}/products/{_product_id()}/licenses/deactivate.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"uid": instance_id, "license_key": license_key},
                headers={"Accept": "application/json"},
            )

        if resp.status_code >= 400:
            try:
                payload = resp.json()
                err = payload.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                msg = f"HTTP {resp.status_code}"
            return DeactivateResult(ok=False, error=msg or "Deactivation failed.")

        return DeactivateResult(ok=True)
