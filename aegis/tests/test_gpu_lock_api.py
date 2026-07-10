"""API tests for the GPU lock router (§5.2 single-GPU multi-project sharing)."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import gpu_lock as router_mod
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.runtime.config import AegisSettings, get_settings
from aegis.server.services import gpu_lock as gl


@pytest.fixture(autouse=True)
def _reset_gate():
    gl._gate = None
    gl._gate_size = None
    gl._holders.clear()
    gl._expiry_tasks.clear()
    yield
    gl._gate = None
    gl._gate_size = None
    gl._holders.clear()
    gl._expiry_tasks.clear()


@pytest.fixture(autouse=True)
def _no_real_gpu_query():
    """/status also queries real GPU processes via docker exec — stub it out so
    unit tests don't depend on an actual docker daemon / GPU exporter container."""
    with mock.patch.object(router_mod.gpu_processes, "list_active_processes", return_value=[]):
        yield


def _cfg(**over) -> AegisSettings:
    base = dict(
        ollama_gateway_token="",
        ollama_gateway_max_concurrency=1,
    )
    base.update(over)
    return AegisSettings(**base)  # type: ignore[call-arg]


def _client(cfg: AegisSettings) -> tuple[TestClient, mock.AsyncMock]:
    fa = FastAPI()
    fa.include_router(router_mod.router)
    fa.dependency_overrides[get_settings] = lambda: cfg

    async def _user() -> UserContext:
        return UserContext(user_id=uuid.uuid4(), email="t@x.com", orgs=[])

    conn = mock.AsyncMock()

    async def _conn():
        yield conn

    fa.dependency_overrides[get_current_user] = _user
    fa.dependency_overrides[get_db_conn] = _conn
    return TestClient(fa, raise_server_exceptions=False), conn


def test_token_configured_rejects_missing_header():
    client, _ = _client(_cfg(ollama_gateway_token="secret"))
    r = client.post("/api/v1/gpu/lock/acquire", json={"owner": "ocr-vllm"})
    assert r.status_code == 401


def test_acquire_then_release_roundtrip():
    client, _ = _client(_cfg())
    r = client.post("/api/v1/gpu/lock/acquire", json={"owner": "ocr-vllm", "lease_sec": 30})
    assert r.status_code == 200
    token = r.json()["token"]

    r2 = client.post("/api/v1/gpu/lock/release", json={"token": token})
    assert r2.status_code == 200
    assert r2.json() == {"released": True}


def test_release_unknown_token_reports_false():
    client, _ = _client(_cfg())
    r = client.post("/api/v1/gpu/lock/release", json={"token": "bogus"})
    assert r.status_code == 200
    assert r.json() == {"released": False}


def test_acquire_busy_returns_503():
    client, _ = _client(_cfg())
    first = client.post("/api/v1/gpu/lock/acquire", json={"owner": "a", "lease_sec": 30})
    assert first.status_code == 200

    second = client.post(
        "/api/v1/gpu/lock/acquire",
        json={"owner": "b", "lease_sec": 30, "queue_timeout_sec": 0.05},
    )
    assert second.status_code == 503

    client.post("/api/v1/gpu/lock/release", json={"token": first.json()["token"]})


def test_status_reports_empty_when_idle():
    client, _ = _client(_cfg())
    r = client.get("/api/v1/gpu/lock/status")
    assert r.status_code == 200
    assert r.json() == {"gate_size": 0, "holders": [], "active_processes": []}


def test_status_reports_current_holder():
    client, _ = _client(_cfg())
    client.post("/api/v1/gpu/lock/acquire", json={"owner": "ocr-vllm", "lease_sec": 30})

    r = client.get("/api/v1/gpu/lock/status")
    assert r.status_code == 200
    body = r.json()
    assert body["gate_size"] == 1
    assert len(body["holders"]) == 1
    assert body["holders"][0]["owner"] == "ocr-vllm"
    assert body["holders"][0]["lease_sec"] == 30


def test_status_reports_active_gpu_processes():
    from aegis.server.services.gpu_processes import GpuProcess

    client, _ = _client(_cfg())
    with mock.patch.object(
        router_mod.gpu_processes,
        "list_active_processes",
        return_value=[
            GpuProcess(
                pid=123, process_name="VLLM::EngineCore", memory_bytes=9074, container="ocr-vllm"
            )
        ],
    ):
        r = client.get("/api/v1/gpu/lock/status")
    assert r.status_code == 200
    body = r.json()
    assert body["active_processes"] == [
        {
            "pid": 123,
            "process_name": "VLLM::EngineCore",
            "memory_bytes": 9074,
            "container": "ocr-vllm",
        }
    ]


def test_acquire_success_records_gpu_lock_acquired_metric():
    client, conn = _client(_cfg())
    r = client.post("/api/v1/gpu/lock/acquire", json={"owner": "ocr-vllm", "lease_sec": 30})
    assert r.status_code == 200

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert args[2] == "gpu_lock_acquired"
    assert '"owner": "ocr-vllm"' in args[5]


def test_acquire_busy_records_gpu_lock_acquire_busy_metric():
    client, _ = _client(_cfg())
    first = client.post("/api/v1/gpu/lock/acquire", json={"owner": "a", "lease_sec": 30})

    client2, conn2 = _client(_cfg())
    # share the same in-process gate — max_concurrency=1 across both clients since gl
    # module state is process-global, not per-FastAPI-app.
    second = client2.post(
        "/api/v1/gpu/lock/acquire",
        json={"owner": "b", "lease_sec": 30, "queue_timeout_sec": 0.05},
    )
    assert second.status_code == 503

    conn2.execute.assert_awaited_once()
    args = conn2.execute.await_args.args
    assert args[2] == "gpu_lock_acquire_busy"
    assert '"owner": "b"' in args[5]

    client.post("/api/v1/gpu/lock/release", json={"token": first.json()["token"]})
