"""Security posture — scan running containers for risky configuration (CSPM-lite).
Flags privileged mode, docker-socket / host-root mounts, host network, root user,
mutable :latest tags, and broadly-published ports. Deeper CVE/runtime scanning is
available by installing Trivy / Falco from the App Store."""

from __future__ import annotations

import json
import subprocess
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/security", tags=["security"])


def _docker(args: list[str]) -> str:
    return subprocess.run(  # noqa: S603
        ["docker", "-H", get_settings().docker_host, *args],  # noqa: S607
        capture_output=True, text=True, timeout=20, check=False,
    ).stdout


def _scan_one(c: dict[str, Any]) -> list[dict[str, str]]:
    f: list[dict[str, str]] = []
    hc = c.get("HostConfig", {})
    cfg = c.get("Config", {})
    name = c.get("Name", "").lstrip("/")
    if hc.get("Privileged"):
        f.append({"sev": "high", "check": "privileged", "detail": "runs in privileged mode"})
    if hc.get("NetworkMode") == "host":
        f.append({"sev": "medium", "check": "host_network", "detail": "uses host network"})
    for m in c.get("Mounts", []):
        src = m.get("Source", "")
        if src == "/var/run/docker.sock":
            f.append({"sev": "high", "check": "docker_socket", "detail": "mounts docker.sock (host takeover risk)"})
        elif src == "/" and m.get("RW"):
            f.append({"sev": "high", "check": "host_root_rw", "detail": "mounts host / read-write"})
    if not cfg.get("User"):
        f.append({"sev": "low", "check": "root_user", "detail": "no non-root USER set"})
    img = cfg.get("Image", "")
    if img.endswith(":latest") or (":" not in img.split("/")[-1]):
        f.append({"sev": "low", "check": "mutable_tag", "detail": f"uses mutable tag: {img}"})
    for hostport in (hc.get("PortBindings") or {}).values():
        for b in hostport or []:
            if b.get("HostIp") in ("", "0.0.0.0", "::"):
                f.append({"sev": "medium", "check": "exposed_port",
                          "detail": f"port published on all interfaces: {b.get('HostPort')}"})
    return [dict(x, container=name) for x in f]


@router.get("/posture")
async def posture(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    names = [n for n in _docker(["ps", "--format", "{{.Names}}"]).split() if n]
    findings: list[dict[str, str]] = []
    for n in names[:80]:
        raw = _docker(["inspect", n])
        try:
            data = json.loads(raw)
            if data:
                findings.extend(_scan_one(data[0]))
        except Exception:  # noqa: BLE001
            continue
    by_sev = {"high": 0, "medium": 0, "low": 0}
    for x in findings:
        by_sev[x["sev"]] = by_sev.get(x["sev"], 0) + 1
    findings.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}[x["sev"]])
    return {"scanned": len(names), "summary": by_sev, "findings": findings}
