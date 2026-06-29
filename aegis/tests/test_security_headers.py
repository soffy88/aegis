"""Tests for SecurityHeadersMiddleware."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from aegis.server.middleware.security_headers import SecurityHeadersMiddleware


async def _ok(request: object) -> JSONResponse:
    return JSONResponse({"ok": True})


def _client(hsts: bool) -> TestClient:
    app = Starlette(routes=[Route("/x", _ok)])
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return TestClient(app)


def test_base_headers_present() -> None:
    r = _client(hsts=False).get("/x")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_hsts_only_when_enabled() -> None:
    assert "Strict-Transport-Security" not in _client(hsts=False).get("/x").headers
    assert "max-age" in _client(hsts=True).get("/x").headers["Strict-Transport-Security"]
