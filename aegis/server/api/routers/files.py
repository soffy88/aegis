"""Host filesystem file-manager API (sandboxed to AEGIS_FILE_MANAGER_ROOTS).

Read/browse endpoints require ``VIEW_PROJECT`` (viewer+). Mutating endpoints
(write/mkdir/upload/rename) require ``TRIGGER_AUTOHEAL`` (operator+). Delete
requires ``INSTALL_APP`` (member+). Every path is validated against the
configured whitelist by :mod:`aegis.server.services.files`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.services import file_shares as sharesvc
from aegis.server.services import files as filesvc
from aegis.server.services.files import (
    FileManagerDisabled,
    PathNotAllowed,
    ThumbnailUnavailable,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/files", tags=["files"])


def _map(exc: Exception) -> HTTPException:
    """Translate service-layer exceptions into HTTP errors."""
    if isinstance(exc, PathNotAllowed):
        return HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, FileManagerDisabled):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (NotADirectoryError, IsADirectoryError, ValueError)):
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    log.exception("file manager unexpected error")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


class WriteRequest(BaseModel):
    path: str
    content: str


class PathRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    src: str
    dst: str


class ChmodRequest(BaseModel):
    path: str
    mode: str


class CompressRequest(BaseModel):
    paths: list[str]
    dest: str


class ExtractRequest(BaseModel):
    path: str
    dest_dir: str


@router.get("/roots")
async def list_roots(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, list[str]]:
    """List the configured whitelist roots. Empty means the feature is off."""
    return {"roots": filesvc.get_roots()}


@router.get("/list")
async def list_dir(
    org_id: UUID,
    path: str = Query(..., description="Absolute directory path within a whitelist root"),
    show_hidden: bool = Query(default=True),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.list_dir, path, show_hidden=show_hidden)
    except Exception as exc:
        raise _map(exc) from exc


@router.get("/read")
async def read_file(
    org_id: UUID,
    path: str = Query(...),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.read_text, path)
    except Exception as exc:
        raise _map(exc) from exc


@router.get("/download")
async def download_file(
    org_id: UUID,
    path: str = Query(...),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> FileResponse:
    try:
        p = await asyncio.to_thread(filesvc.resolve_for_download, path)
    except Exception as exc:
        raise _map(exc) from exc
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@router.get("/thumbnail")
async def thumbnail(
    org_id: UUID,
    path: str = Query(...),
    size: int = Query(default=256, ge=16, le=1024),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> Response:
    """Return a webp thumbnail for a sandboxed image file. viewer+."""
    try:
        data, media = await asyncio.to_thread(
            filesvc.generate_thumbnail, path, max_size=size, fmt="webp"
        )
    except ThumbnailUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:
        raise _map(exc) from exc
    return Response(
        content=data, media_type=media, headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get("/media")
async def media_info(
    org_id: UUID,
    path: str = Query(...),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Return media metadata (duration/codec/resolution) for a file. viewer+."""
    try:
        return await asyncio.to_thread(filesvc.probe_media, path)
    except ThumbnailUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:
        raise _map(exc) from exc


class ShareRequest(BaseModel):
    path: str
    expires_in_hours: int | None = None
    max_downloads: int | None = None


@router.post("/share", status_code=status.HTTP_201_CREATED)
async def create_share(
    org_id: UUID,
    req: ShareRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Create a time-limited public share link for a file. operator+ required."""
    try:
        return await sharesvc.create_share(
            conn,
            org_id=org_id,
            path=req.path,
            created_by=user.user_id,
            expires_in_hours=req.expires_in_hours,
            max_downloads=req.max_downloads,
        )
    except Exception as exc:
        raise _map(exc) from exc


@router.get("/shares")
async def list_shares(
    org_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List active/expired share links for the org."""
    return await sharesvc.list_shares(conn, org_id=org_id)


@router.delete("/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    org_id: UUID,
    share_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> None:
    """Revoke a share link. operator+ required."""
    ok = await sharesvc.revoke_share(conn, org_id=org_id, share_id=share_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="share not found")


@router.put("/write")
async def write_file(
    org_id: UUID,
    body: WriteRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.write_text, body.path, body.content)
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/mkdir", status_code=status.HTTP_201_CREATED)
async def make_dir(
    org_id: UUID,
    body: PathRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.make_dir, body.path)
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    org_id: UUID,
    dir: str = Form(..., description="Target directory (absolute, within a root)"),
    file: UploadFile = File(...),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    data = await file.read()
    try:
        return await asyncio.to_thread(
            filesvc.upload_file, dir, file.filename or "upload.bin", data
        )
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/rename")
async def rename_file(
    org_id: UUID,
    body: RenameRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.rename_path, body.src, body.dst)
    except Exception as exc:
        raise _map(exc) from exc


@router.delete("/delete")
async def delete_file(
    org_id: UUID,
    path: str = Query(...),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.delete_path, path)
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/chmod")
async def chmod_file(
    org_id: UUID,
    body: ChmodRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.change_mode, body.path, body.mode)
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/compress")
async def compress_files(
    org_id: UUID,
    body: CompressRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.compress, body.paths, body.dest)
    except Exception as exc:
        raise _map(exc) from exc


@router.post("/extract")
async def extract_archive(
    org_id: UUID,
    body: ExtractRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(filesvc.extract, body.path, body.dest_dir)
    except Exception as exc:
        raise _map(exc) from exc
