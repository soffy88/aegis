"""Users self-profile endpoints. M1: self-service only."""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.repositories import UserRepository

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UserProfileResponse(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    default_org_id: UUID | None
    is_active: bool
    created_at: str
    last_login_at: str | None


class UpdateProfileRequest(BaseModel):
    display_name: str | None = Field(None, max_length=100)
    default_org_id: UUID | None = None


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    user: UserContext = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> UserProfileResponse:
    user_repo = UserRepository(conn)
    u = await user_repo.get_by_id(user.user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return UserProfileResponse(**u.to_api_dict())


@router.patch("/me", response_model=UserProfileResponse)
async def update_my_profile(
    req: UpdateProfileRequest,
    user: UserContext = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> UserProfileResponse:
    if req.default_org_id and not user.org_by_id(req.default_org_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "default_org_id must be an org you belong to",
        )

    user_repo = UserRepository(conn)
    updated = await user_repo.update_profile(
        user.user_id,
        display_name=req.display_name,
        default_org_id=req.default_org_id,
    )
    return UserProfileResponse(**updated.to_api_dict())
