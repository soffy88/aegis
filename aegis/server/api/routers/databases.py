"""Database management — create / list / drop databases on DB-engine app
containers the org installed from the store (Postgres / MySQL / MariaDB).

Scoped to the org's own installed_apps rows so it never touches unrelated
platform databases. Credentials are read from the container's own env; SQL runs
via `docker exec` inside the target container.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/databases", tags=["databases"])

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")  # safe SQL identifier


def _engine(image: str) -> str | None:
    img = (image or "").lower()
    if "postgres" in img or "timescale" in img:
        return "postgres"
    if "mariadb" in img or "mysql" in img:
        return "mysql"
    return None


def _container_env(name: str, docker_host: str) -> dict[str, str]:
    """Read a container's env via `docker inspect` (backend ships the docker CLI)."""
    try:
        out = subprocess.run(  # noqa: S603
            ["docker", "-H", docker_host, "inspect", name, "--format", "{{json .Config.Env}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        pairs = json.loads(out) if out else []
        env: dict[str, str] = {}
        for item in pairs or []:
            k, _, v = item.partition("=")
            env[k] = v
        return env
    except Exception:  # noqa: BLE001
        return {}


def _exec(name: str, cmd: list[str], docker_host: str, env: dict[str, str] | None = None) -> str:
    """Run a command inside the container; raise 400 on non-zero exit."""
    from oprim import docker_container_exec  # noqa: PLC0415

    res = docker_container_exec(
        container_id=name, command=cmd, env=env, timeout_sec=30, docker_host=docker_host
    )
    if getattr(res, "exit_code", 1) != 0:
        out = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"db command failed: {out[:300]}")
    return getattr(res, "stdout", "") or ""


def _sql_list(name: str, engine: str, env: dict[str, str], dh: str) -> list[str]:
    if engine == "postgres":
        user = env.get("POSTGRES_USER", "postgres")
        out = _exec(
            name,
            [
                "psql",
                "-U",
                user,
                "-tAc",
                "SELECT datname FROM pg_database WHERE datistemplate=false",
            ],
            dh,
            {"PGPASSWORD": env.get("POSTGRES_PASSWORD", "")},
        )
        skip = {"template0", "template1"}
    else:
        pw = env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD", "")
        out = _exec(
            name,
            [
                "sh",
                "-c",
                "mariadb -uroot -N -e 'SHOW DATABASES' 2>/dev/null || mysql -uroot -N -e 'SHOW DATABASES'",
            ],
            dh,
            {"MYSQL_PWD": pw},
        )
        skip = {"information_schema", "performance_schema", "mysql", "sys"}
    return [d for d in (line.strip() for line in out.splitlines()) if d and d not in skip]


class CreateDbRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=63)


async def _org_db_instances(conn: asyncpg.Connection, org_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT app_name, image FROM installed_apps WHERE org_id = $1 AND status = 'completed'",
        org_id,
    )
    dh = get_settings().docker_host
    out: list[dict[str, Any]] = []
    for r in rows:
        eng = _engine(r["image"] or "")
        if not eng:
            continue
        env = await asyncio.to_thread(_container_env, r["app_name"], dh)
        has_creds = bool(
            env.get("POSTGRES_PASSWORD")
            or env.get("MARIADB_ROOT_PASSWORD")
            or env.get("MYSQL_ROOT_PASSWORD")
            or eng == "postgres"
        )
        out.append(
            {"name": r["app_name"], "engine": eng, "image": r["image"], "manageable": has_creds}
        )
    return out


async def _resolve(conn: asyncpg.Connection, org_id: uuid.UUID, name: str) -> dict[str, Any]:
    for inst in await _org_db_instances(conn, org_id):
        if inst["name"] == name:
            return inst
    raise HTTPException(status.HTTP_404_NOT_FOUND, "database instance not found in this org")


@router.get("")
async def list_instances(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List the org's installed Postgres/MySQL/MariaDB instances."""
    return await _org_db_instances(conn, org_id)


@router.get("/{name}/dbs")
async def list_databases(
    org_id: uuid.UUID,
    name: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[str]:
    inst = await _resolve(conn, org_id, name)
    dh = get_settings().docker_host
    env = await asyncio.to_thread(_container_env, name, dh)
    return await asyncio.to_thread(_sql_list, name, inst["engine"], env, dh)


@router.post("/{name}/dbs", status_code=status.HTTP_201_CREATED)
async def create_database(
    org_id: uuid.UUID,
    name: str,
    req: CreateDbRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, str]:
    if not _IDENT.match(req.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid database name")
    inst = await _resolve(conn, org_id, name)
    dh = get_settings().docker_host
    env = await asyncio.to_thread(_container_env, name, dh)

    def _do() -> None:
        if inst["engine"] == "postgres":
            user_name = env.get("POSTGRES_USER", "postgres")
            _exec(
                name,
                ["psql", "-U", user_name, "-c", f'CREATE DATABASE "{req.name}"'],
                dh,
                {"PGPASSWORD": env.get("POSTGRES_PASSWORD", "")},
            )
        else:
            pw = env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD", "")
            _exec(
                name,
                [
                    "sh",
                    "-c",
                    f"mariadb -uroot -e 'CREATE DATABASE `{req.name}`' 2>/dev/null || "
                    f"mysql -uroot -e 'CREATE DATABASE `{req.name}`'",
                ],
                dh,
                {"MYSQL_PWD": pw},
            )

    await asyncio.to_thread(_do)
    return {"status": "created", "name": req.name}


@router.delete("/{name}/dbs/{db}", status_code=status.HTTP_204_NO_CONTENT)
async def drop_database(
    org_id: uuid.UUID,
    name: str,
    db: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    if not _IDENT.match(db):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid database name")
    inst = await _resolve(conn, org_id, name)
    dh = get_settings().docker_host
    env = await asyncio.to_thread(_container_env, name, dh)

    def _do() -> None:
        if inst["engine"] == "postgres":
            user_name = env.get("POSTGRES_USER", "postgres")
            _exec(
                name,
                ["psql", "-U", user_name, "-c", f'DROP DATABASE "{db}"'],
                dh,
                {"PGPASSWORD": env.get("POSTGRES_PASSWORD", "")},
            )
        else:
            pw = env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD", "")
            _exec(
                name,
                [
                    "sh",
                    "-c",
                    f"mariadb -uroot -e 'DROP DATABASE `{db}`' 2>/dev/null || "
                    f"mysql -uroot -e 'DROP DATABASE `{db}`'",
                ],
                dh,
                {"MYSQL_PWD": pw},
            )

    await asyncio.to_thread(_do)
