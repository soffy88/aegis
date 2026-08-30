"""Configuration Management — Data models for desired state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class PackageState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    LATEST = "latest"


class ServiceState(StrEnum):
    STARTED = "started"
    STOPPED = "stopped"
    RESTARTED = "restarted"
    RELOADED = "reloaded"
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass
class PackageSpec:
    name: str
    state: PackageState = PackageState.PRESENT
    version: str | None = None
    manager: str = "apt"  # apt, yum, dnf, apk, pacman


@dataclass
class ServiceSpec:
    name: str
    state: ServiceState = ServiceState.STARTED
    enabled: bool | None = None


@dataclass
class FileSpec:
    path: str
    content: str | None = None
    template: str | None = None  # Jinja2 template name
    template_vars: dict[str, Any] = field(default_factory=dict)
    mode: str = "0644"
    owner: str = "root"
    group: str = "root"
    backup: bool = True


@dataclass
class SysctlSpec:
    key: str
    value: str | int
    persistent: bool = True  # write to /etc/sysctl.d/


@dataclass
class UserSpec:
    name: str
    state: PackageState = PackageState.PRESENT
    groups: list[str] = field(default_factory=list)
    shell: str = "/bin/bash"
    home: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    password_hash: str | None = None


@dataclass
class DesiredState:
    packages: list[PackageSpec] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    files: list[FileSpec] = field(default_factory=list)
    sysctl: list[SysctlSpec] = field(default_factory=list)
    users: list[UserSpec] = field(default_factory=list)
    
    # 扩展点：自定义资源类型
    custom: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class DiffEntry:
    resource_type: str
    resource_name: str
    action: str  # create, update, delete, noop
    current: dict[str, Any] | None
    desired: dict[str, Any]
    changes: dict[str, tuple[Any, Any]]  # key -> (old, new)


@dataclass
class ExecutionResult:
    success: bool
    diffs: list[DiffEntry] = field(default_factory=list)
    applied: list[DiffEntry] = field(default_factory=list)
    failed: list[DiffEntry] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    rollback_available: bool = False