"""RBAC immediate-revocation via users.token_epoch.

get_current_user rejects an access token whose `epoch` claim no longer matches the
DB (role change / removal / deactivate / password change bump it), or whose user is
missing/inactive. These use a mock conn so they run without a real Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from obase.auth import jwt_sign_hs256

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers.auth import _issue_access_token
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.runtime.config import get_settings

_UID = uuid.uuid4()


def _app_and_conn(db_epoch: int | None) -> tuple[TestClient, mock.AsyncMock]:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(get_current_user)) -> dict:
        return {"user_id": str(user.user_id)}

    conn = mock.AsyncMock()
    conn.fetchval.return_value = db_epoch  # SELECT token_epoch ... AND is_active

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False), conn


def _token(epoch: int) -> str:
    tok, _ = _issue_access_token(_UID, "u@x.com", [], epoch)
    return tok


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_matching_epoch_accepted() -> None:
    client, _ = _app_and_conn(db_epoch=3)
    r = client.get("/whoami", headers=_auth(_token(3)))
    assert r.status_code == 200
    assert r.json()["user_id"] == str(_UID)


def test_stale_epoch_rejected() -> None:
    """DB epoch bumped past the token (e.g. role changed) → old token is dead."""
    client, _ = _app_and_conn(db_epoch=4)
    r = client.get("/whoami", headers=_auth(_token(3)))
    assert r.status_code == 401


def test_missing_or_inactive_user_rejected() -> None:
    """fetchval returns None when the user is gone or is_active=false."""
    client, _ = _app_and_conn(db_epoch=None)
    r = client.get("/whoami", headers=_auth(_token(0)))
    assert r.status_code == 401


def test_pre_migration_token_without_epoch_claim_rejected() -> None:
    """A token minted before this change carries no epoch claim → rejected against
    the DB default 0, forcing exactly one re-auth after deploy."""
    tok = jwt_sign_hs256(
        payload={"sub": str(_UID), "email": "u@x.com", "orgs": [], "type": "access"},
        secret=get_settings().jwt_secret,
        expires_in_seconds=300,
    )
    client, _ = _app_and_conn(db_epoch=0)
    r = client.get("/whoami", headers=_auth(tok))
    assert r.status_code == 401


def test_access_token_carries_epoch_claim() -> None:
    """_issue_access_token must embed the epoch so the check has something to compare."""
    from obase.auth import jwt_verify_hs256

    tok, _ = _issue_access_token(_UID, "u@x.com", [], 7)
    payload = jwt_verify_hs256(token=tok, secret=get_settings().jwt_secret, check_exp=True)
    assert payload["epoch"] == 7


@pytest.mark.asyncio
async def test_repo_bumps_epoch_on_revoking_changes() -> None:
    """Deactivation and password change increment token_epoch; reactivation does not."""
    from aegis.server.repositories.user_repo import UserRepository

    conn = mock.AsyncMock()
    repo = UserRepository(conn)

    await repo.bump_token_epoch(_UID)
    assert "token_epoch = token_epoch + 1" in conn.execute.await_args.args[0]

    await repo.update_password(_UID, "newhash")
    assert "token_epoch = token_epoch + 1" in conn.execute.await_args.args[0]

    await repo.set_active(_UID, is_active=False)
    assert "token_epoch = token_epoch + 1" in conn.execute.await_args.args[0]

    await repo.set_active(_UID, is_active=True)
    assert "token_epoch" not in conn.execute.await_args.args[0]  # reactivation must not bump
