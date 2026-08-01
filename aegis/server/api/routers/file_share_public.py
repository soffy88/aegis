"""Public file-share download route — NO authentication.

`GET /s/{token}` streams the file behind a valid share token. The token itself
is the capability; there is no org/user context. All validation (revoked /
expired / over-limit / path still allowed) happens in
:func:`file_shares.resolve_share`, which fails closed. Kept in its own router so
it sits OUTSIDE the `/api/v1/orgs/{org_id}` authenticated tree.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from aegis.server.api.deps import get_db_conn
from aegis.server.services import file_shares as sharesvc
from aegis.server.services.file_shares import ShareNotValid

router = APIRouter(prefix="/s", tags=["share"])


@router.get("/{token}")
async def download_shared(
    token: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> FileResponse:
    """Download the file behind a share token. Public, capability-gated."""
    try:
        path, filename = await sharesvc.resolve_share(conn, token=token)
    except ShareNotValid as exc:
        # 404 (not 403) so an invalid/expired token is indistinguishable from a
        # never-existent one — no oracle on which tokens once existed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return FileResponse(path, filename=filename, media_type="application/octet-stream")
