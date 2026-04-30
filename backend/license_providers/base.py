"""
License provider protocol — normalized interface that license.py talks to.

Each provider (LemonSqueezy, Freemius, etc.) implements LicenseProvider and
translates its vendor-specific request/response shapes into the dataclasses
below. license.py never sees vendor-specific fields.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ActivateResult:
    ok: bool
    instance_id: str = ""
    customer_name: str = ""
    customer_email: str = ""
    error: str = ""
    activation_limit_reached: bool = False


@dataclass
class ValidateResult:
    """
    ok=True means the provider confirmed the key is known and the call succeeded.
    status is the normalized lifecycle: "active" | "expired" | "disabled" | "invalid".
    Network/transport failures should be raised as exceptions, not returned here.
    """
    ok: bool
    status: str = ""
    error: str = ""


@dataclass
class DeactivateResult:
    ok: bool
    error: str = ""


class LicenseProvider(Protocol):
    name: str

    async def activate(self, license_key: str, install_uuid: str) -> ActivateResult: ...

    async def validate(self, license_key: str, instance_id: str) -> ValidateResult: ...

    async def deactivate(self, license_key: str, instance_id: str) -> DeactivateResult: ...
