"""Configuration Management — Declarative desired state + idempotent executor.

AEGIS_DESIGN v1.1.0 §7
"""

from __future__ import annotations

from .executor import ConfigExecutor, ExecutionResult, DesiredState
from .models import (
    PackageSpec,
    ServiceSpec,
    FileSpec,
    SysctlSpec,
    UserSpec,
    DesiredState,
)

__all__ = [
    "ConfigExecutor",
    "ExecutionResult",
    "DesiredState",
    "PackageSpec",
    "ServiceSpec",
    "FileSpec",
    "SysctlSpec",
    "UserSpec",
]