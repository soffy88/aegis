"""Kubernetes read-only viewer — nodes / namespaces / pods / deployments / events
from a configured kubeconfig (AEGIS_KUBECONFIG). Aegis is Docker-native; this adds
observability into a K8s cluster without shipping kubectl (talks the REST API)."""

from __future__ import annotations

import base64
import os
import ssl
import tempfile
import uuid
from functools import lru_cache
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/k8s", tags=["k8s"])


@lru_cache(maxsize=1)
def _kube() -> dict[str, Any] | None:
    """Parse the kubeconfig once → {server, ssl, headers}."""
    path = get_settings().kubeconfig
    if not path or not os.path.exists(path):
        return None
    cfg = yaml.safe_load(open(path))
    ctx_name = cfg.get("current-context")
    ctx = next(c["context"] for c in cfg["contexts"] if c["name"] == ctx_name)
    cluster = next(c["cluster"] for c in cfg["clusters"] if c["name"] == ctx["cluster"])
    usr = next(u["user"] for u in cfg["users"] if u["name"] == ctx["user"])

    def _write(data_b64: str, suffix: str) -> str:
        fd, p = tempfile.mkstemp(suffix=suffix)
        os.write(fd, base64.b64decode(data_b64))
        os.close(fd)
        return p

    ca = cluster.get("certificate-authority-data")
    sslctx: Any
    if cluster.get("insecure-skip-tls-verify"):
        sslctx = ssl.create_default_context()
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE
    elif ca:
        sslctx = ssl.create_default_context(cafile=_write(ca, ".ca"))
    else:
        sslctx = ssl.create_default_context()

    headers: dict[str, str] = {}
    if usr.get("token"):
        headers["Authorization"] = f"Bearer {usr['token']}"
    elif usr.get("client-certificate-data") and usr.get("client-key-data"):
        sslctx.load_cert_chain(
            certfile=_write(usr["client-certificate-data"], ".crt"),
            keyfile=_write(usr["client-key-data"], ".key"),
        )
    return {
        "server": cluster["server"].rstrip("/"),
        "ssl": sslctx,
        "headers": headers,
    }


def _client() -> tuple[dict[str, Any], httpx.Client]:
    k = _kube()
    if not k:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "kubeconfig not configured (AEGIS_KUBECONFIG)"
        )
    return k, httpx.Client(verify=k["ssl"], headers=k["headers"], timeout=12)


def _get(path: str) -> dict[str, Any]:
    k, c = _client()
    try:
        r = c.get(f"{k['server']}{path}")
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"k8s api: {exc}") from exc
    finally:
        c.close()


@router.get("/status")
async def k8s_status(
    org_id: uuid.UUID, user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT))
) -> dict[str, Any]:
    if not _kube():
        return {"configured": False}
    try:
        v = _get("/version")
        return {"configured": True, "reachable": True, "version": v.get("gitVersion")}
    except HTTPException as exc:
        return {"configured": True, "reachable": False, "detail": exc.detail}


@router.get("/nodes")
async def k8s_nodes(
    org_id: uuid.UUID, user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT))
) -> list[dict[str, Any]]:
    items = _get("/api/v1/nodes").get("items", [])
    out = []
    for n in items:
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        out.append(
            {
                "name": n["metadata"]["name"],
                "ready": conds.get("Ready") == "True",
                "kubelet": n["status"].get("nodeInfo", {}).get("kubeletVersion"),
                "os": n["status"].get("nodeInfo", {}).get("osImage"),
            }
        )
    return out


@router.get("/namespaces")
async def k8s_namespaces(
    org_id: uuid.UUID, user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT))
) -> list[str]:
    return [n["metadata"]["name"] for n in _get("/api/v1/namespaces").get("items", [])]


@router.get("/pods")
async def k8s_pods(
    org_id: uuid.UUID,
    ns: str = Query(default="default"),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    items = _get(f"/api/v1/namespaces/{ns}/pods").get("items", [])
    out = []
    for p in items:
        st = p.get("status", {})
        cs = st.get("containerStatuses", []) or []
        ready = sum(1 for c in cs if c.get("ready"))
        restarts = sum(c.get("restartCount", 0) for c in cs)
        out.append(
            {
                "name": p["metadata"]["name"],
                "phase": st.get("phase"),
                "ready": f"{ready}/{len(cs)}",
                "restarts": restarts,
                "node": p.get("spec", {}).get("nodeName"),
            }
        )
    return out


@router.get("/deployments")
async def k8s_deployments(
    org_id: uuid.UUID,
    ns: str = Query(default="default"),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    items = _get(f"/apis/apps/v1/namespaces/{ns}/deployments").get("items", [])
    return [
        {
            "name": d["metadata"]["name"],
            "ready": f"{d.get('status', {}).get('readyReplicas', 0)}/{d.get('spec', {}).get('replicas', 0)}",
            "image": (
                d["spec"]["template"]["spec"]["containers"][0]["image"]
                if d["spec"]["template"]["spec"].get("containers")
                else ""
            ),
        }
        for d in items
    ]


@router.get("/events")
async def k8s_events(
    org_id: uuid.UUID,
    ns: str = Query(default="default"),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    items = _get(f"/api/v1/namespaces/{ns}/events").get("items", [])
    return [
        {
            "type": e.get("type"),
            "reason": e.get("reason"),
            "object": f"{e.get('involvedObject', {}).get('kind')}/{e.get('involvedObject', {}).get('name')}",
            "message": e.get("message", "")[:200],
            "count": e.get("count", 1),
        }
        for e in items[-100:]
    ]
