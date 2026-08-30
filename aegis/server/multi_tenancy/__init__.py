"""Multi-Tenancy — Resource quotas and isolation enforcement."""

from __future__ import annotations

from .quotas import QuotaManager, QuotaExceededError

__all__ = [
    "QuotaManager",
    "QuotaExceededError",
]