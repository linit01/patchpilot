"""
LemonSqueezy license provider.

Wraps the LS REST API:
  POST /v1/licenses/activate
  POST /v1/licenses/validate
  POST /v1/licenses/deactivate
"""
import httpx

from .base import ActivateResult, DeactivateResult, ValidateResult

LS_API_URL = "https://api.lemonsqueezy.com/v1/licenses"


class LemonSqueezyProvider:
    name = "lemonsqueezy"

    async def activate(self, license_key: str, install_uuid: str) -> ActivateResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{LS_API_URL}/activate",
                data={"license_key": license_key, "instance_name": install_uuid},
                headers={"Accept": "application/json"},
            )
            result = resp.json()

        error_msg = result.get("error") or ""
        if error_msg or not result.get("activated"):
            return ActivateResult(
                ok=False,
                error=error_msg or "Activation failed. Please check your license key.",
                activation_limit_reached="activation limit" in error_msg.lower(),
            )

        instance = result.get("instance") or {}
        meta = result.get("meta") or {}
        return ActivateResult(
            ok=True,
            instance_id=instance.get("id", ""),
            customer_name=meta.get("customer_name", ""),
            customer_email=meta.get("customer_email", ""),
        )

    async def validate(self, license_key: str, instance_id: str) -> ValidateResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{LS_API_URL}/validate",
                data={"license_key": license_key, "instance_id": instance_id},
                headers={"Accept": "application/json"},
            )
            result = resp.json()

        if not result.get("valid"):
            return ValidateResult(
                ok=False,
                status="invalid",
                error=result.get("error", "Validation failed."),
            )

        ls_status = (result.get("license_key") or {}).get("status", "active")
        if ls_status in ("expired", "disabled"):
            return ValidateResult(ok=True, status=ls_status)
        return ValidateResult(ok=True, status="active")

    async def deactivate(self, license_key: str, instance_id: str) -> DeactivateResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{LS_API_URL}/deactivate",
                data={"license_key": license_key, "instance_id": instance_id},
                headers={"Accept": "application/json"},
            )
            result = resp.json()

        error_msg = result.get("error") or ""
        return DeactivateResult(ok=not error_msg, error=error_msg)
