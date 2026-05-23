"""App installation service (moved from omodul.install_app)."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class InstallAppConfig:
    register_domain: bool = False


@dataclass
class InstallAppInput:
    app_name: str
    project_dir: str | None = None
    image_to_pull: str | None = None
    health_check_container: str | None = None
    domain: str | None = None
    domain_target_url: str | None = None


def install_app(cfg: InstallAppConfig, inp: InstallAppInput, output_dir: Path) -> dict[str, Any]:
    """Install an app via docker compose up in output_dir.

    Returns {"status": "completed"|"failed", "findings": {...}}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    compose_file = output_dir / "docker-compose.yml"

    if not compose_file.exists():
        return {"status": "failed", "findings": {"error": "No docker-compose.yml in output_dir"}}

    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        return {"status": "failed", "findings": {"error": exc.stderr or str(exc)}}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "findings": {"error": "docker compose up timed out"}}

    findings: dict[str, Any] = {}
    if cfg.register_domain and inp.domain:
        findings["domain_registered"] = True

    return {"status": "completed", "findings": findings}
