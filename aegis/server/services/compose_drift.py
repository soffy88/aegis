"""§10/§3.7 config-as-code 对账 —— 声明态 vs 运行态漂移检测.

多数生产事故由变更引发(§10.1),故 git 声明态与实际运行态的漂移 MUST 被发现并写成一等
change 事件。这里以 installed_apps.image(声明镜像)对比运行容器镜像,消费
oskill.compose_drift_detect,漂移逐 app 写 config.drift 事件(与遥测同库同模型)。

命名约定:声明侧 key=app_name,运行侧 key=容器名(去前导 /)。匹配靠同名;不匹配的运行容器
计为 orphan(removed,无声明来源),声明有但没跑的计为 added(app 缺失/未起)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


def build_declared(app_rows: list[Any]) -> dict[str, dict[str, str]]:
    """installed_apps 行 → {app_name: {'image': image}}(仅有镜像的行)。"""
    return {r["app_name"]: {"image": r["image"]} for r in app_rows if r["image"]}


def build_running(containers: list[Any]) -> dict[str, dict[str, str]]:
    """ContainerInfo 列表 → {name: {'image': image}}(name 去前导 /)。"""
    return {
        (c.name or "").lstrip("/"): {"image": c.image}
        for c in containers
        if (c.name or "").lstrip("/")
    }


async def scan_drift(conn: Any, cfg: Any) -> dict[str, Any]:
    """比对声明态与运行态,漂移逐 app 写 config.drift 事件。返回漂移摘要。

    docker 不可达/无声明应用 → 视为无漂移跳过(不崩循环)。"""
    from obase.docker import docker_container_list  # noqa: PLC0415
    from oskill.compose_drift_detect import compose_drift_detect  # noqa: PLC0415

    app_rows = await conn.fetch(
        "SELECT app_name, org_id, project_id, image FROM installed_apps "
        "WHERE image IS NOT NULL AND status = 'running'"
    )
    declared = build_declared(app_rows)
    if not declared:
        return {"in_sync": True, "checked": 0}

    try:
        containers = await asyncio.to_thread(
            docker_container_list, all=False, docker_host=cfg.docker_host
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("compose_drift_docker_error err=%s (skip)", exc)
        return {"in_sync": True, "checked": len(declared), "docker_error": str(exc)[:120]}

    running = build_running(containers)
    drift = compose_drift_detect(declared=declared, running=running, compare_fields=["image"])
    if drift.in_sync:
        return {"in_sync": True, "checked": len(declared)}

    # 逐 app 写 config.drift 一等变更事件(§10.1)。removed=运行侧孤儿,无声明 app 归属 → 仅日志。
    await _record_drift_events(conn, app_rows, drift)
    log.warning(
        "compose_drift changed=%s missing(added)=%s orphan(removed)=%s",
        [c.service for c in drift.changed],
        drift.added,
        drift.removed,
    )
    return {
        "in_sync": False,
        "checked": len(declared),
        "changed": [c.service for c in drift.changed],
        "added": list(drift.added),
        "removed": list(drift.removed),
    }


async def _record_drift_events(conn: Any, app_rows: list[Any], drift: Any) -> None:
    from aegis.server.persistence import append_event  # noqa: PLC0415

    meta = {r["app_name"]: r for r in app_rows}
    changed_by_svc = {c.service: c for c in drift.changed}
    # 声明侧漂移项:镜像变更 + 声明有但没跑(added)
    for name in set(changed_by_svc) | set(drift.added):
        row = meta.get(name)
        if row is None:
            continue
        sd = changed_by_svc.get(name)
        payload: dict[str, Any] = (
            {"kind": "image_changed", "declared": sd.declared, "running": sd.running}
            if sd is not None
            else {"kind": "not_running", "declared": row["image"]}
        )
        try:
            await append_event(
                conn=conn,
                org_id=row["org_id"],
                project_id=row["project_id"],
                event_type="config.drift",
                severity="warning",
                service=name,
                resource=name,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("compose_drift_event_error app=%s err=%s", name, exc)
