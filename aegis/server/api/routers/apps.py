"""Installed apps management API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import get_pool
from aegis.server.persistence.event_trail import append_event
from aegis.server.repositories.project_repo import ProjectRepository
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/apps", tags=["apps"])


class InstallRequest(BaseModel):
    app_name: str
    app_version: str | None = None
    install_dir: str = Field(..., min_length=1)
    image_to_pull: str | None = None
    health_check_container: str | None = None
    domain: str | None = None
    domain_target_url: str | None = None
    register_domain: bool = False
    node_id: uuid.UUID | None = None  # target node; omit to install on the platform host
    host_port: int | None = None  # published host port for compose apps (auto-freed if taken)
    params: dict[str, str] = {}  # values for the catalog entry's declared install params

    @field_validator("install_dir")
    @classmethod
    def install_dir_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("install_dir is required")
        return v


class InstallAppRequest(BaseModel):
    """Internal request model for _run_install (catalog spec pre-resolved)."""

    app_name: str
    app_version: str | None = None
    image_to_pull: str | None = None
    ports: list[dict[str, Any]] = []
    env: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    command: list[str] | None = None
    domain: str | None = None
    target_host: str = "localhost"
    docker_host: str = "unix:///var/run/docker.sock"
    # Multi-container apps: a docker-compose YAML shipped in the catalog entry.
    # When set, install runs `docker compose up` instead of a single container.
    compose: str | None = None
    compose_env: dict[str, str] = {}


def _build_container_spec(body: InstallAppRequest) -> dict[str, Any]:
    """Translate a catalog app spec into oprim docker_container_create kwargs."""
    ports: dict[str, int] = {}
    for p in body.ports or []:
        cp = p.get("container_port")
        if cp:
            ports[f"{cp}/{p.get('protocol', 'tcp')}"] = int(cp)
    env: dict[str, str] = {}
    for e in body.env or []:
        k = e.get("name") or e.get("key")
        if k:
            env[str(k)] = "" if e.get("value") is None else str(e.get("value"))
    volumes: dict[str, dict[str, str]] = {}
    for m in body.mounts or []:
        vn, target = m.get("volume_name"), m.get("target")
        if vn and target:
            volumes[str(vn)] = {"bind": str(target), "mode": "rw"}
    return {
        "ports": ports or None,
        "env": env or None,
        "volumes": volumes or None,
        "command": body.command or None,
    }


def _pick_free_host_port(preferred: int, docker_host: str) -> int:
    """Return `preferred` if no container already publishes it on the host, else the
    next free port above it. Best-effort — falls back to `preferred` on any error."""
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    used: set[int] = set()
    try:
        out = subprocess.run(  # noqa: S603
            ["docker", "-H", docker_host, "ps", "--format", "{{.Ports}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        for m in re.finditer(r":(\d+)->", out):
            used.add(int(m.group(1)))
    except Exception:  # noqa: BLE001
        return preferred
    port = preferred
    while port in used and port < preferred + 500:
        port += 1
    return port


def _compose_install(body: InstallAppRequest, data_dir: Path | None, docker_host: str) -> None:
    """Materialize a multi-container app's compose file + .env under the data dir
    and bring it up. Images are pulled on demand (compose pull='missing')."""
    import secrets  # noqa: PLC0415

    from oprim import docker_compose_up  # noqa: PLC0415

    base = (data_dir or Path("/data/aegis")) / "apps" / body.app_name
    base.mkdir(parents=True, exist_ok=True)
    compose_path = base / "docker-compose.yml"
    compose_path.write_text(body.compose or "")

    # .env is written once and reused so generated secrets stay stable across
    # restarts/re-installs. Sentinel values get freshly generated on first write:
    #   __RANDOM__     -> url-safe token (generic secret / password)
    #   __HEX32__      -> 32 hex chars (e.g. Laravel-style APP_KEY without prefix)
    #   __APP_KEY_B64__-> "base64:"+b64(32 bytes) (Laravel/BookStack APP_KEY)
    import base64  # noqa: PLC0415

    def _gen(v: str) -> str:
        if v == "__RANDOM__":
            return secrets.token_urlsafe(24)
        if v == "__HEX32__":
            return secrets.token_hex(16)
        if v == "__APP_KEY_B64__":
            return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
        return v

    env_path = base / ".env"
    if not env_path.exists():
        lines = [f"{k}={_gen(v)}" for k, v in (body.compose_env or {}).items()]
        env_path.write_text("\n".join(lines) + ("\n" if lines else ""))

    docker_compose_up(
        compose_file=str(compose_path),
        project_name=body.app_name,
        pull="missing",
        docker_host=docker_host,
    )


async def _run_install(
    install_id: uuid.UUID,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    trace_id: str,
    body: InstallAppRequest,
    data_dir: Path | None = None,
) -> None:
    """Background task: pull the image and create+start the container from the
    catalog spec (image/ports/env/mounts). A real single-container deploy."""
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    final_status: str = "failed"
    error_detail: str | None = None
    domain: str | None = None
    dh = body.docker_host

    try:
        if body.compose:
            # Multi-container app: materialize compose + .env and bring it up.
            await asyncio.to_thread(_compose_install, body, data_dir, dh)
            final_status = "completed"
            domain = body.domain
        else:
            from oprim import (  # noqa: PLC0415
                docker_container_create,
                docker_container_start,
                docker_image_pull,
            )

            image = body.image_to_pull
            if not image:
                raise RuntimeError("no container image resolved for this app")

            # 1. Pull the image (best-effort — create can still use a local copy).
            try:
                base, _, tag = image.partition(":")
                await asyncio.to_thread(
                    docker_image_pull, image=base, tag=tag or "latest", docker_host=dh
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("install_pull_warn image=%s err=%s", image, exc)

            spec = _build_container_spec(body)
            # 2. Create the container (on re-install the name exists → just start it).
            try:
                await asyncio.to_thread(
                    docker_container_create,
                    image=image,
                    name=body.app_name,
                    labels={"aegis.managed": "true", "aegis.app": body.app_name},
                    restart_policy="unless-stopped",
                    network=cfg.app_install_network or None,
                    docker_host=dh,
                    **spec,
                )
            except Exception as exc:  # noqa: BLE001 — usually "name already in use"
                log.info(
                    "install_create_skipped app=%s (%s) — starting existing", body.app_name, exc
                )

            # 3. Start it.
            await asyncio.to_thread(
                docker_container_start, container_id=body.app_name, docker_host=dh
            )
            final_status = "completed"
            domain = body.domain
    except Exception as exc:  # noqa: BLE001
        log.exception("install_failed install_id=%s", install_id)
        error_detail = f"{type(exc).__name__}: {exc}"

    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE installed_apps
                   SET status = $1, domain = $2
                 WHERE id = $3
                """,
                final_status,
                domain,
                install_id,
            )
            try:
                await append_event(
                    conn=conn,
                    org_id=org_id,
                    project_id=project_id,
                    event_type="omodul_run",
                    severity="info" if final_status == "completed" else "warning",
                    resource=f"app/{body.app_name}",
                    omodul_kind="install_app",
                    payload={
                        "install_id": str(install_id),
                        "final_status": final_status,
                        "error_detail": error_detail,
                    },
                    trace_id=trace_id,
                    initiated_by="agent",
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to append event_trail for install %s", install_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to update installed_apps status for %s", install_id)


@router.get("")
async def list_apps(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List installed apps. project_id=None returns all in this org."""
    rows = await conn.fetch(
        """
        SELECT id, app_name, app_version, install_dir, domain, status, installed_at
          FROM installed_apps
         WHERE org_id = $1 AND ($2::uuid IS NULL OR project_id = $2)
         ORDER BY installed_at DESC
        """,
        org_id,
        project_id,
    )
    return [dict(r) for r in rows]


@router.get("/{install_id}")
async def get_app(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get an installed app by ID. viewer+ can read."""
    row = await conn.fetchrow(
        "SELECT * FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    return dict(row)


@router.get("/{install_id}/history")
async def app_version_history(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Full upgrade/rollback history for an app (newest first). viewer+ can read."""
    owns = await conn.fetchval(
        "SELECT 1 FROM installed_apps WHERE id = $1 AND org_id = $2", install_id, org_id
    )
    if not owns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    rows = await conn.fetch(
        "SELECT from_version, to_version, action, created_at"
        " FROM app_version_history WHERE install_id = $1 ORDER BY created_at DESC",
        install_id,
    )
    return [dict(r) for r in rows]


@router.post("/install", status_code=status.HTTP_202_ACCEPTED)
async def install_app_endpoint(
    org_id: uuid.UUID,
    req: InstallRequest,
    background_tasks: BackgroundTasks,
    project_id: uuid.UUID = Query(..., description="Project to install the app into"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Install an app into a project. member+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )

    install_dir = req.install_dir
    cfg_data_dir = get_settings().data_dir

    # Resolve the container image from the store catalog when the caller didn't
    # supply one (the console install form doesn't send image_to_pull). Previously
    # the catalog image was ignored entirely, so installs had nothing to pull.
    from aegis.server.api.routers.store import find_catalog_app  # noqa: PLC0415

    entry = find_catalog_app(req.app_name) or {}
    image_to_pull = req.image_to_pull or entry.get("image")

    install_id = await conn.fetchval(
        """
        INSERT INTO installed_apps
            (org_id, project_id, app_name, app_version, install_dir, status, image)
        VALUES ($1, $2, $3, $4, $5, 'installing', $6)
        ON CONFLICT (org_id, project_id, app_name)
            DO UPDATE SET status = 'installing', installed_at = now(), image = EXCLUDED.image
        RETURNING id
        """,
        org_id,
        project_id,
        req.app_name,
        req.app_version,
        install_dir,
        image_to_pull,
    )

    # Resolve the install target. Default: the platform's own daemon. With a
    # node_id, install onto that node's host/daemon instead of hardcoded localhost.
    target_host = "localhost"
    docker_host = get_settings().docker_host
    if req.node_id is not None:
        node = await conn.fetchrow(
            "SELECT host, docker_host_url FROM aegis_nodes WHERE org_id = $1 AND node_id = $2",
            org_id,
            req.node_id,
        )
        if not node:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
        target_host = node["host"]
        docker_host = node["docker_host_url"] or docker_host

    # Compose apps publish a host port. Honor a caller-supplied port, else the
    # catalog default, then bump to a free port so the install never fails on a
    # port collision. host_port stays None for single-container apps.
    compose_env = dict(entry.get("compose_env") or {})
    # Merge user-supplied install params (only keys the catalog entry declared).
    declared = {p.get("key") for p in (entry.get("params") or []) if p.get("key")}
    for k, v in (req.params or {}).items():
        if k in declared and v != "":
            compose_env[k] = v
    host_port: int | None = None
    if entry.get("compose") and compose_env.get("HOST_PORT"):
        preferred = req.host_port or int(compose_env["HOST_PORT"])
        host_port = _pick_free_host_port(preferred, docker_host)
        compose_env["HOST_PORT"] = str(host_port)

    body = InstallAppRequest(
        app_name=req.app_name,
        app_version=req.app_version,
        image_to_pull=image_to_pull,
        ports=entry.get("ports", []),
        env=entry.get("env", []),
        mounts=entry.get("mounts", []),
        command=entry.get("command"),
        compose=entry.get("compose"),
        compose_env=compose_env,
        domain=req.domain,
        target_host=target_host,
        docker_host=docker_host,
    )
    task_trace_id = f"trc_{uuid.uuid4().hex[:8]}"
    background_tasks.add_task(
        _run_install, install_id, org_id, project_id, task_trace_id, body, cfg_data_dir
    )

    return {"install_id": str(install_id), "status": "installing", "host_port": host_port}


@router.delete("/{install_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_app(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    """Uninstall an app. member+ required.

    Best-effort stops the running container before dropping the row so uninstall
    doesn't leave it running (the old code only deleted the DB record). The
    container is named after the app instance. Full removal (docker rm + volume +
    Caddy route cleanup) needs an uninstall primitive the 3O libs don't expose yet.
    """
    row = await conn.fetchrow(
        "SELECT app_name FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    dh = get_settings().docker_host
    compose_file = Path(get_settings().data_dir) / "apps" / row["app_name"] / "docker-compose.yml"
    if compose_file.exists():
        # Multi-container app: tear the whole stack down (containers + network).
        try:
            from oprim import docker_compose_down  # noqa: PLC0415

            await asyncio.to_thread(
                docker_compose_down,
                compose_file=str(compose_file),
                project_name=row["app_name"],
                docker_host=dh,
            )
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            log.warning("uninstall_compose_down_failed app=%s err=%s", row["app_name"], exc)
    else:
        try:
            from oprim import docker_container_stop  # noqa: PLC0415

            await asyncio.to_thread(
                docker_container_stop, container_id=row["app_name"], docker_host=dh
            )
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            log.warning("uninstall_stop_failed app=%s err=%s", row["app_name"], exc)

    await conn.execute(
        "DELETE FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )


class ComposeUpdateRequest(BaseModel):
    compose: str = Field(..., min_length=1)


async def _compose_path_for(
    conn: asyncpg.Connection, org_id: uuid.UUID, install_id: uuid.UUID
) -> tuple[str, Path]:
    row = await conn.fetchrow(
        "SELECT app_name FROM installed_apps WHERE id = $1 AND org_id = $2", install_id, org_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found")
    path = Path(get_settings().data_dir) / "apps" / row["app_name"] / "docker-compose.yml"
    return row["app_name"], path


@router.get("/{install_id}/compose")
async def get_compose(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Return the compose file of a multi-container app (None for single-container)."""
    app_name, path = await _compose_path_for(conn, org_id, install_id)
    return {
        "app_name": app_name,
        "is_compose": path.exists(),
        "compose": path.read_text() if path.exists() else None,
    }


@router.put("/{install_id}/compose")
async def update_compose(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    req: ComposeUpdateRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, str]:
    """Overwrite the compose file and redeploy the stack (`docker compose up -d`)."""
    app_name, path = await _compose_path_for(conn, org_id, install_id)
    if not path.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a compose app")
    path.write_text(req.compose)

    def _redeploy() -> None:
        from oprim import docker_compose_up  # noqa: PLC0415

        docker_compose_up(
            compose_file=str(path),
            project_name=app_name,
            pull="missing",
            docker_host=get_settings().docker_host,
        )

    try:
        await asyncio.to_thread(_redeploy)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"redeploy failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {"status": "redeployed"}


@router.post("/{install_id}/backup")
async def backup_app_endpoint(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    target: str = Query(default="s3", description="s3 | webdav"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Tar the app's data directory and upload it to S3 or WebDAV storage."""
    import datetime as _dt  # noqa: PLC0415

    from aegis.server.services import remote_backup  # noqa: PLC0415

    row = await conn.fetchrow(
        "SELECT app_name FROM installed_apps WHERE id = $1 AND org_id = $2", install_id, org_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found")
    cfg = get_settings()
    fn = remote_backup.backup_app_webdav if target == "webdav" else remote_backup.backup_app
    configured = (
        remote_backup.webdav_configured(cfg)
        if target == "webdav"
        else remote_backup.is_configured(cfg)
    )
    if not configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{target} backup storage is not configured"
        )
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        return await asyncio.to_thread(
            fn, str(org_id), row["app_name"], cfg, Path(cfg.data_dir), stamp
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"backup failed: {type(exc).__name__}: {exc}"
        ) from exc


class UpgradeRequest(BaseModel):
    target_version: str = Field(..., min_length=1, max_length=100)


async def _record_version_transition(
    conn: asyncpg.Connection,
    *,
    install_id: uuid.UUID,
    from_version: str | None,
    to_version: str | None,
    action: str,
) -> None:
    """Append an immutable version-transition row (audit #19).

    `installed_apps.previous_version` only remembers one level; this table keeps the
    full upgrade/rollback history so provenance survives repeated transitions.
    """
    await conn.execute(
        "INSERT INTO app_version_history (install_id, from_version, to_version, action)"
        " VALUES ($1, $2, $3, $4)",
        install_id,
        from_version,
        to_version or "",
        action,
    )


async def _dispatch_upgrade(
    *,
    app_name: str,
    current_version: str | None,
    target_version: str,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Invoke omodul.upgrade_self_hosted_app via the dispatcher (mirrors _run_install)."""
    from aegis.server.dispatch.budget_tracker import BudgetTracker  # noqa: PLC0415
    from aegis.server.dispatch.dedup_cache import DedupCache  # noqa: PLC0415
    from aegis.server.dispatch.omodul_dispatcher import OmodulDispatcher  # noqa: PLC0415

    import redis.asyncio as aioredis  # noqa: PLC0415

    cfg = get_settings()
    redis_client = aioredis.from_url(cfg.redis_url)
    try:
        dispatcher = OmodulDispatcher(
            DedupCache(redis_client),
            BudgetTracker(redis_client),
            data_dir=str(cfg.data_dir),
        )
        return await dispatcher.invoke(
            omodul_name="upgrade_self_hosted_app",
            config={
                "instance_name": app_name,
                "current_version": current_version or "",
                "target_version": target_version,
            },
            input_data={
                "container_id": "",
                "new_image": "",
                "docker_host": cfg.docker_host,
            },
            user_id=str(org_id),
            project_id=project_id,
        )
    finally:
        await redis_client.aclose()


def _run_rollback(*, app_name: str, rollback_to_version: str) -> dict[str, Any]:
    """Invoke omodul.rollback_app directly (sync; mirrors the autoheal engine)."""
    import tempfile  # noqa: PLC0415

    from omodul.rollback_app import (  # noqa: PLC0415
        RollbackAppConfig,
        RollbackAppInput,
        rollback_app,
    )

    cfg = get_settings()
    config = RollbackAppConfig(
        app_slug=app_name,
        instance_name=app_name,
        rollback_to_version=rollback_to_version,
        restore_data=True,
    )
    input_data = RollbackAppInput(
        docker_host=cfg.docker_host,
        backup_bucket=cfg.backup_s3_bucket,
        aws_endpoint_url=cfg.backup_s3_endpoint_url,
    )
    with tempfile.TemporaryDirectory() as tmp:
        return rollback_app(config, input_data, Path(tmp))


async def _run_app_lifecycle(
    *,
    omodul_name: str,
    app_name: str,
    target_version: str,
    install_id: uuid.UUID,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    current_version: str | None = None,
) -> None:
    """Background task: actually execute the upgrade/rollback via omodul.

    Replaces the prior log-only stub. The endpoint does version bookkeeping
    synchronously; this runs the real omodul call and then marks the row
    `active` (status=="completed") or `failed`, with the truthful error. Never
    raises (background task).

    Note: upgrade's container_id/new_image are not yet tracked on installed_apps,
    so the upgrade omodul may report failed until image tracking lands (#19). That
    is still strictly more honest than the old stub, which always marked active.
    """
    from aegis.server.persistence.db import get_pool  # noqa: PLC0415

    status_after = "active"
    error_detail: str | None = None
    try:
        if omodul_name == "upgrade_self_hosted_app":
            result = await _dispatch_upgrade(
                app_name=app_name,
                current_version=current_version,
                target_version=target_version,
                org_id=org_id,
                project_id=project_id,
            )
        else:
            result = await asyncio.to_thread(
                _run_rollback, app_name=app_name, rollback_to_version=target_version
            )
        if str(result.get("status")) != "completed":
            status_after = "failed"
            error_detail = str(result.get("error")) if result.get("error") else "not completed"
    except Exception as exc:  # noqa: BLE001
        log.warning("app_lifecycle_failed omodul=%s app=%s err=%s", omodul_name, app_name, exc)
        status_after = "failed"
        error_detail = f"{type(exc).__name__}: {exc}"

    if error_detail:
        log.warning(
            "app_lifecycle_outcome id=%s status=%s err=%s", install_id, status_after, error_detail
        )
    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE installed_apps SET status = $2 WHERE id = $1", install_id, status_after
            )
    except Exception:  # noqa: BLE001
        log.warning("app_lifecycle_status_update_failed id=%s", install_id)


@router.post("/{install_id}/upgrade", status_code=status.HTTP_202_ACCEPTED)
async def upgrade_app(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    req: UpgradeRequest,
    background_tasks: BackgroundTasks,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Upgrade an installed app, remembering the prior version for rollback. member+."""
    app = await conn.fetchrow(
        "SELECT app_name, app_version FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    await conn.execute(
        "UPDATE installed_apps SET previous_version = app_version, app_version = $3,"
        " status = 'upgrading' WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
        req.target_version,
    )
    await _record_version_transition(
        conn,
        install_id=install_id,
        from_version=app["app_version"],
        to_version=req.target_version,
        action="upgrade",
    )
    background_tasks.add_task(
        _run_app_lifecycle,
        omodul_name="upgrade_self_hosted_app",
        app_name=app["app_name"],
        target_version=req.target_version,
        install_id=install_id,
        org_id=org_id,
        current_version=app["app_version"],
    )
    return {
        "install_id": str(install_id),
        "status": "upgrading",
        "from_version": app["app_version"],
        "to_version": req.target_version,
    }


@router.post("/{install_id}/rollback", status_code=status.HTTP_202_ACCEPTED)
async def rollback_app_endpoint(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Roll an app back to its previous version. member+."""
    app = await conn.fetchrow(
        "SELECT app_name, app_version, previous_version FROM installed_apps"
        " WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    if not app["previous_version"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="no previous version to roll back to"
        )
    # Swap current <-> previous so a second rollback returns to where we were.
    await conn.execute(
        "UPDATE installed_apps SET app_version = previous_version,"
        " previous_version = app_version, status = 'rolling_back'"
        " WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    await _record_version_transition(
        conn,
        install_id=install_id,
        from_version=app["app_version"],
        to_version=app["previous_version"],
        action="rollback",
    )
    background_tasks.add_task(
        _run_app_lifecycle,
        omodul_name="rollback_app",
        app_name=app["app_name"],
        target_version=app["previous_version"],
        install_id=install_id,
        org_id=org_id,
    )
    return {
        "install_id": str(install_id),
        "status": "rolling_back",
        "rolled_back_to": app["previous_version"],
    }
