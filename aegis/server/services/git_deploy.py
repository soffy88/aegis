"""Build-and-deploy a container straight from a Git repository.

Coolify/Dokploy-style: shallow-clone a public git repo, build its Dockerfile via
the Docker daemon, then create+start the resulting image as a managed container.
Single-container; the repo must contain a Dockerfile (optionally under a subdir).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess  # noqa: S404 — git clone with a validated URL, no shell
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
_SAFE_SUBDIR = re.compile(r"^[A-Za-z0-9._/-]{0,200}$")


def _validate(repo_url: str, app_name: str, branch: str | None, subdir: str | None) -> None:
    if not (repo_url.startswith("http://") or repo_url.startswith("https://")):
        raise ValueError("repo_url must be an http(s) git URL")
    if not _SAFE_NAME.match(app_name):
        raise ValueError("app_name must be lowercase alphanumeric with . _ - (max 63)")
    if branch and not _SAFE_BRANCH.match(branch):
        raise ValueError("invalid branch name")
    if subdir and (not _SAFE_SUBDIR.match(subdir) or ".." in subdir):
        raise ValueError("invalid subdir")


def _clone_and_build(
    repo_url: str, branch: str | None, app_name: str, subdir: str | None, build_root: str
) -> str:
    """Blocking: shallow-clone the repo and docker-build it. Returns the image tag."""
    import docker  # noqa: PLC0415

    dest = Path(build_root) / app_name
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", repo_url, str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed in the server image") from exc
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed: {(r.stderr or r.stdout).strip()[:300]}")

    ctx = dest / subdir if subdir else dest
    if not (ctx / "Dockerfile").exists():
        shutil.rmtree(dest, ignore_errors=True)
        loc = f"subdir '{subdir}'" if subdir else "repo root"
        raise RuntimeError(f"no Dockerfile found at {loc}")

    tag = f"aegis-git/{app_name}:latest"
    client = docker.from_env()
    try:
        client.images.build(path=str(ctx), tag=tag, rm=True, forcerm=True)
    except docker.errors.BuildError as exc:  # type: ignore[attr-defined]
        raise RuntimeError(f"docker build failed: {str(exc)[:300]}") from exc
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    return tag


async def build_and_deploy_from_git(
    *,
    repo_url: str,
    branch: str | None,
    app_name: str,
    subdir: str | None,
    ports: list[int] | None,
    env: list[dict[str, Any]] | None,
    docker_host: str,
    build_root: str,
) -> str:
    """Validate, build from git, then create+start the container. Returns image tag."""
    _validate(repo_url, app_name, branch, subdir)
    tag = await asyncio.to_thread(
        _clone_and_build, repo_url, branch, app_name, subdir, build_root
    )

    from oprim import docker_container_create, docker_container_start  # noqa: PLC0415

    port_map = {f"{p}/tcp": int(p) for p in (ports or [])}
    env_map = {
        str(e["name"]): "" if e.get("value") is None else str(e.get("value"))
        for e in (env or [])
        if e.get("name")
    }
    try:
        await asyncio.to_thread(
            docker_container_create,
            image=tag,
            name=app_name,
            ports=port_map or None,
            env=env_map or None,
            labels={"aegis.managed": "true", "aegis.source": "git", "aegis.app": app_name},
            restart_policy="unless-stopped",
            docker_host=docker_host,
        )
    except Exception as exc:  # noqa: BLE001 — usually "name already in use" on redeploy
        log.info("git_deploy_create_skipped app=%s (%s) — starting existing", app_name, exc)

    await asyncio.to_thread(docker_container_start, container_id=app_name, docker_host=docker_host)
    return tag
