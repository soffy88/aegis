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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.services import files as filesvc
from aegis.server.services.files import FileManagerDisabled, PathNotAllowed

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
