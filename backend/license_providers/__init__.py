"""
License provider package.

Pick the active provider via PATCHPILOT_LICENSE_PROVIDER (default: freemius).
LemonSqueezy support is retained for any operator who wants to opt back in
via the env var, but freemius is the default the project ships with as of
v0.19.0-beta.
"""
import logging
import os

from .base import (
    ActivateResult,
    DeactivateResult,
    LicenseProvider,
    ValidateResult,
)
from .freemius import FreemiusProvider
from .lemonsqueezy import LemonSqueezyProvider

logger = logging.getLogger(__name__)

_PROVIDERS = {
    "lemonsqueezy": LemonSqueezyProvider,
    "freemius": FreemiusProvider,
}

_instance: LicenseProvider | None = None


def get_provider() -> LicenseProvider:
    global _instance
    if _instance is not None:
        return _instance

    name = os.getenv("PATCHPILOT_LICENSE_PROVIDER", "freemius").strip().lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        logger.warning(
            "Unknown PATCHPILOT_LICENSE_PROVIDER=%r — falling back to freemius", name
        )
        cls = FreemiusProvider
    _instance = cls()
    logger.info("License provider: %s", _instance.name)
    return _instance


__all__ = [
    "ActivateResult",
    "DeactivateResult",
    "LicenseProvider",
    "ValidateResult",
    "get_provider",
]
