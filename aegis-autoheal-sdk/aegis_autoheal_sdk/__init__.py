"""aegis-autoheal-sdk — stable contract for Aegis AutoHeal plugins.

This package defines the interfaces an AutoHeal plugin codes against:

- ``Severity``            — deployment environment (drives approval gating)
- ``ActionResultStatus`` / ``ActionResult`` — the outcome of an action
- ``ServiceInfo``         — read-only view of the target service
- ``AutoHealContext``     — capabilities a plugin may invoke (docker restart,
                            http probe, secret read, trail event, …). The host
                            (aegis backend) provides a concrete implementation.
- ``AutoHealPlugin``      — base class plugins subclass; lifecycle is
                            pre_check → execute → post_verify → rollback.

The host wires concrete subclasses (AegisAutoHealContext / AegisServiceInfo);
plugin packages (e.g. aegis-plugins) only depend on these abstractions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "Severity",
    "ActionResultStatus",
    "ActionResult",
    "ServiceInfo",
    "AutoHealContext",
    "AutoHealPlugin",
]


class Severity(StrEnum):
    """Environment severity — used to decide whether an action needs approval."""

    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class ActionResultStatus(StrEnum):
    """Terminal status of a plugin action."""

    OK = "ok"
    FAILED = "failed"
    ESCALATE = "escalate"
    SKIPPED = "skipped"


@dataclass
class ActionResult:
    """Outcome of ``AutoHealPlugin.execute`` / ``rollback``.

    Construct via the factory helpers rather than the raw constructor:
    ``ActionResult.ok(...)`` / ``.failed(...)`` / ``.escalate(...)`` / ``.skipped(...)``.
    """

    status: ActionResultStatus
    detail: str = ""
    escalate_to: str | None = None

    # ── factories ────────────────────────────────────────────────────────────
    @classmethod
    def ok(cls, detail: str = "") -> ActionResult:
        return cls(status=ActionResultStatus.OK, detail=detail)

    @classmethod
    def failed(cls, detail: str = "") -> ActionResult:
        return cls(status=ActionResultStatus.FAILED, detail=detail)

    @classmethod
    def escalate(cls, *, to: str = "human", detail: str = "") -> ActionResult:
        return cls(status=ActionResultStatus.ESCALATE, detail=detail, escalate_to=to)

    @classmethod
    def skipped(cls, detail: str = "") -> ActionResult:
        return cls(status=ActionResultStatus.SKIPPED, detail=detail)

    # ── convenience ──────────────────────────────────────────────────────────
    @property
    def is_success(self) -> bool:
        return self.status == ActionResultStatus.OK


class ServiceInfo(ABC):
    """Read-only view of the service an action targets."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def health(self) -> str: ...

    @property
    @abstractmethod
    def version(self) -> str | None: ...


class AutoHealContext(ABC):
    """Capabilities the host exposes to a plugin during an action.

    The host (aegis backend) provides a concrete subclass. Action methods default
    to raising NotImplementedError so a host implements only what it supports;
    the four context properties are required.
    """

    # ── required context ─────────────────────────────────────────────────────
    @property
    @abstractmethod
    def service(self) -> ServiceInfo: ...

    @property
    @abstractmethod
    def alert_payload(self) -> dict[str, Any]: ...

    @property
    @abstractmethod
    def org_environment(self) -> Severity: ...

    @property
    @abstractmethod
    def trace_id(self) -> str: ...

    # ── capabilities (host implements the subset it supports) ────────────────
    async def systemctl_restart(self, service: str) -> None:
        raise NotImplementedError("systemctl_restart not supported by this host")

    async def kill_process(self, *, name: str | None = None, pid: int | None = None) -> None:
        raise NotImplementedError("kill_process not supported by this host")

    async def docker_restart(self, container: str) -> None:
        raise NotImplementedError("docker_restart not supported by this host")

    async def k8s_pod_delete(self, *, namespace: str, pod: str) -> None:
        raise NotImplementedError("k8s_pod_delete not supported by this host")

    async def http_get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("http_get not supported by this host")

    async def alert_human(self, message: str, *, channel: str = "slack") -> None:
        raise NotImplementedError("alert_human not supported by this host")

    async def get_secret(self, path: str) -> str:
        raise NotImplementedError("get_secret not supported by this host")

    async def emit_trail_event(
        self,
        *,
        event_type: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError("emit_trail_event not supported by this host")


class AutoHealPlugin:
    """Base class for AutoHeal plugins.

    Subclasses set the class attributes (name/version/matches_alert/description,
    optionally rate_limit and requires_approval_when) and override the async
    lifecycle methods. The engine runs them as: pre_check → execute → post_verify,
    falling back to rollback on failure.
    """

    name: str = ""
    version: str = "0.0.0"
    matches_alert: str = ""
    description: str = ""
    # Optional: token-bucket spec like "2/5min" (advisory; engine may enforce).
    rate_limit: str | None = None
    # Optional: Severity at/above which the action needs human approval.
    requires_approval_when: Severity | None = None

    @classmethod
    def validate_config(cls) -> None:
        """Validate the plugin's declared metadata; raise ValueError if misconfigured.

        Checks the always-required identity attributes and, when present, the
        rate_limit format ("<count>/<window>", e.g. "2/5min").
        """
        for attr in ("name", "version", "matches_alert"):
            value = getattr(cls, attr, "")
            if not isinstance(value, str) or not value:
                raise ValueError(f"{cls.__name__}: '{attr}' must be a non-empty string")
        if cls.rate_limit is not None:
            if not isinstance(cls.rate_limit, str) or "/" not in cls.rate_limit:
                raise ValueError(
                    f"{cls.__name__}: rate_limit must look like '<count>/<window>' "
                    f"(got {cls.rate_limit!r})"
                )

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        """Return True if the action should proceed. Default: always proceed."""
        return True

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        """Perform the remediation. Subclasses must override."""
        raise NotImplementedError(f"{self.name or type(self).__name__}.execute not implemented")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        """Return True if the service is healthy after the action. Default: True."""
        return True

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        """Undo the action when execute/post_verify fails. Default: no-op skip."""
        return ActionResult.skipped("no rollback implemented")
