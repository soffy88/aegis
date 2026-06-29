"""Tests for AuthRateLimitMiddleware — in-process fallback (redis_url=None)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from aegis.server.middleware.rate_limit import AuthRateLimitMiddleware, _limit_group


async def _ok(request: object) -> JSONResponse:
    return JSONResponse({"ok": True})


def _client(max_requests: int = 3, window_sec: int = 60) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/v1/auth/login", _ok, methods=["POST"]),
            Route("/api/v1/auth/refresh", _ok, methods=["POST"]),
            Route("/api/v1/invites/{token}", _ok, methods=["GET"]),
            Route("/api/v1/health", _ok, methods=["GET"]),
        ]
    )
    app.add_middleware(
        AuthRateLimitMiddleware,
        max_requests=max_requests,
        window_sec=window_sec,
        redis_url=None,
    )
    return TestClient(app)


# ── _limit_group pure logic ────────────────────────────────────────────────────


def test_limit_group_matches_auth_paths() -> None:
    assert _limit_group("/api/v1/auth/login", "POST") == "/api/v1/auth/login"
    assert _limit_group("/api/v1/auth/refresh", "POST") == "/api/v1/auth/refresh"
    assert _limit_group("/api/v1/auth/register", "POST") == "/api/v1/auth/register"


def test_limit_group_collapses_invites() -> None:
    assert _limit_group("/api/v1/invites/abc", "GET") == "invites"
    assert _limit_group("/api/v1/invites/xyz/accept", "POST") == "invites"


def test_limit_group_skips_others() -> None:
    assert _limit_group("/api/v1/health", "GET") is None
    assert _limit_group("/api/v1/auth/login", "GET") is None  # GET not limited


# ── enforcement ────────────────────────────────────────────────────────────────


def test_unlimited_path_never_429() -> None:
    c = _client(max_requests=2)
    for _ in range(10):
        assert c.get("/api/v1/health").status_code == 200


def test_login_limited_after_max() -> None:
    c = _client(max_requests=3)
    for _ in range(3):
        assert c.post("/api/v1/auth/login").status_code == 200
    r = c.post("/api/v1/auth/login")
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"


def test_refresh_is_limited() -> None:
    c = _client(max_requests=2)
    assert c.post("/api/v1/auth/refresh").status_code == 200
    assert c.post("/api/v1/auth/refresh").status_code == 200
    assert c.post("/api/v1/auth/refresh").status_code == 429


def test_invite_token_paths_share_one_bucket() -> None:
    """A rotating token must not reset the limit (enumeration defence)."""
    c = _client(max_requests=2)
    assert c.get("/api/v1/invites/tok-1").status_code == 200
    assert c.get("/api/v1/invites/tok-2").status_code == 200
    assert c.get("/api/v1/invites/tok-3").status_code == 429


def test_separate_ips_get_separate_buckets() -> None:
    c = _client(max_requests=1)
    assert c.post("/api/v1/auth/login", headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 200
    assert c.post("/api/v1/auth/login", headers={"cf-connecting-ip": "2.2.2.2"}).status_code == 200
    assert c.post("/api/v1/auth/login", headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 429
