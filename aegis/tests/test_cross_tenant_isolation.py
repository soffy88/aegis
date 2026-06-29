"""Cross-tenant isolation tests for the org-scoped auth gate.

Every org-scoped route depends on require_permission / require_min_role, which resolve
the membership for the path's org_id from the caller's token. These tests prove a user
authenticated for org A cannot reach org B by injecting B's id into the path.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.auth.rbac import (
    Permission,
    Role,
    require_min_role,
    require_permission,
)

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()


def _app(dep: object) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/orgs/{org_id}/thing")
    async def _route(org_id: uuid.UUID, user: UserContext = Depends(dep)) -> dict[str, str]:
        return {"org_id": str(org_id)}

    return app


def _user(*, org_id: uuid.UUID = _ORG_A, role: str = "admin", orgs: bool = True) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        email="a@example.com",
        orgs=[OrgInToken(org_id=org_id, slug="a", role=role)] if orgs else [],
    )


def _client(app: FastAPI, user: UserContext) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


# ── require_permission ─────────────────────────────────────────────────────────


def test_member_can_access_own_org() -> None:
    app = _app(require_permission(Permission.VIEW_PROJECT))
    r = _client(app, _user(role="viewer")).get(f"/api/v1/orgs/{_ORG_A}/thing")
    assert r.status_code == 200
    assert r.json()["org_id"] == str(_ORG_A)


def test_member_of_a_cannot_access_b_via_path_injection() -> None:
    app = _app(require_permission(Permission.VIEW_PROJECT))
    r = _client(app, _user(org_id=_ORG_A)).get(f"/api/v1/orgs/{_ORG_B}/thing")
    assert r.status_code == 403
    assert "not a member" in r.json()["detail"].lower()


def test_user_with_no_orgs_is_forbidden() -> None:
    app = _app(require_permission(Permission.VIEW_PROJECT))
    r = _client(app, _user(orgs=False)).get(f"/api/v1/orgs/{_ORG_A}/thing")
    assert r.status_code == 403


def test_role_lacking_permission_is_forbidden_same_org() -> None:
    """A viewer in their own org still can't perform an install (perm gate)."""
    app = _app(require_permission(Permission.INSTALL_APP))
    r = _client(app, _user(role="viewer")).get(f"/api/v1/orgs/{_ORG_A}/thing")
    assert r.status_code == 403
    assert "missing permission" in r.json()["detail"].lower()


# ── require_min_role ───────────────────────────────────────────────────────────


def test_min_role_cross_org_forbidden() -> None:
    app = _app(require_min_role(Role.ADMIN))
    r = _client(app, _user(org_id=_ORG_A, role="owner")).get(f"/api/v1/orgs/{_ORG_B}/thing")
    assert r.status_code == 403
    assert "not a member" in r.json()["detail"].lower()


def test_min_role_insufficient_role_same_org() -> None:
    app = _app(require_min_role(Role.ADMIN))
    r = _client(app, _user(role="member")).get(f"/api/v1/orgs/{_ORG_A}/thing")
    assert r.status_code == 403
    assert "requires role" in r.json()["detail"].lower()


def test_min_role_sufficient_role_same_org_ok() -> None:
    app = _app(require_min_role(Role.ADMIN))
    r = _client(app, _user(role="owner")).get(f"/api/v1/orgs/{_ORG_A}/thing")
    assert r.status_code == 200
