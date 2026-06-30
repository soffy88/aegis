"""Orgs CRUD + members management."""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.models import Role
from aegis.server.persistence.audit import record_audit
from aegis.server.repositories import MembershipRepository, OrgRepository, UserRepository

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


# ====== schemas ======


class OrgCreateRequest(BaseModel):
    slug: str = Field(min_length=3, max_length=50, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100)
    plan: str = "free"


class OrgUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)


class OrgResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    plan: str
    status: str
    created_at: str


class MemberResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str | None
    role: str
    joined_at: str


class InviteRequest(BaseModel):
    email: EmailStr
    role: str


class ChangeRoleRequest(BaseModel):
    role: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: UUID


# ====== endpoints ======


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    req: OrgCreateRequest,
    user: UserContext = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> OrgResponse:
    """任何登录 user 都能创建 org, 自动成 owner."""
    org_repo = OrgRepository(conn)
    membership_repo = MembershipRepository(conn)

    existing = await org_repo.get_by_slug(req.slug)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, f"slug '{req.slug}' already exists")

    org = await org_repo.create(slug=req.slug, name=req.name, plan=req.plan)
    await membership_repo.add(user_id=user.user_id, org_id=org.id, role=Role.OWNER)

    return OrgResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        plan=org.plan,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


@router.get("", response_model=list[OrgResponse])
async def list_my_orgs(
    user: UserContext = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> list[OrgResponse]:
    """列出当前 user 所属所有 org. 自己看自己, 不需要 require_permission."""
    org_repo = OrgRepository(conn)
    orgs = await org_repo.list_by_user(user.user_id)
    return [
        OrgResponse(
            id=o.id,
            slug=o.slug,
            name=o.name,
            plan=o.plan,
            status=o.status,
            created_at=o.created_at.isoformat(),
        )
        for o in orgs
    ]


@router.get("/{org_id}", response_model=OrgResponse)
async def get_org(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_ORG)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> OrgResponse:
    org_repo = OrgRepository(conn)
    org = await org_repo.get_by_id(org_id)
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")
    return OrgResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        plan=org.plan,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


@router.patch("/{org_id}", response_model=OrgResponse)
async def update_org(
    org_id: UUID,
    req: OrgUpdateRequest,
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> OrgResponse:
    org_repo = OrgRepository(conn)
    if req.name:
        org = await org_repo.update_name(org_id, req.name)
    else:
        org = await org_repo.get_by_id(org_id)
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")
    return OrgResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        plan=org.plan,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.DELETE_ORG)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> None:
    """删除 org. 级联删 memberships / projects (DB ON DELETE CASCADE)."""
    org_repo = OrgRepository(conn)
    ok = await org_repo.delete(org_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")


# ====== members ======


@router.get("/{org_id}/members", response_model=list[MemberResponse])
async def list_members(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_ORG)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> list[MemberResponse]:
    membership_repo = MembershipRepository(conn)
    rows = await membership_repo.list_by_org_with_users(org_id)
    return [
        MemberResponse(
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=m.role.value,
            joined_at=m.joined_at.isoformat(),
        )
        for m, u in rows
    ]


@router.post("/{org_id}/members", status_code=status.HTTP_201_CREATED)
async def invite_member(
    org_id: UUID,
    req: InviteRequest,
    user: UserContext = Depends(require_permission(Permission.INVITE_USER)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict:
    """邀请 user. M1: 目标 user 必须已注册. M2 加 email 邀请链接."""
    if req.role == Role.OWNER.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "use /transfer-ownership instead")
    try:
        role = Role(req.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid role: {req.role}") from exc

    user_repo = UserRepository(conn)
    target = await user_repo.get_by_email(req.email)
    if not target:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "user not found (M1: target user must register first)",
        )

    membership_repo = MembershipRepository(conn)
    existing = await membership_repo.get(user_id=target.id, org_id=org_id)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "user already in this org")

    await membership_repo.add(user_id=target.id, org_id=org_id, role=role)
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="member.added",
        target_type="user",
        target_id=str(target.id),
        metadata={"email": req.email, "role": req.role},
    )
    return {"message": "user invited", "user_id": str(target.id), "role": req.role}


@router.delete("/{org_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: UUID,
    member_user_id: UUID,
    user: UserContext = Depends(require_permission(Permission.REMOVE_USER)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> None:
    membership_repo = MembershipRepository(conn)
    target = await membership_repo.get(user_id=member_user_id, org_id=org_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    if member_user_id == user.user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "cannot remove yourself; use /leave instead",
        )

    if target.role == Role.OWNER:
        owner_count = await membership_repo.count_owners_in_org(org_id)
        if owner_count <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "cannot remove the only owner; transfer ownership first",
            )

    await membership_repo.remove(user_id=member_user_id, org_id=org_id)
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="member.removed",
        target_type="user",
        target_id=str(member_user_id),
        metadata={"prev_role": target.role.value},
    )


@router.patch("/{org_id}/members/{member_user_id}", response_model=MemberResponse)
async def change_member_role(
    org_id: UUID,
    member_user_id: UUID,
    req: ChangeRoleRequest,
    user: UserContext = Depends(require_permission(Permission.CHANGE_USER_ROLE)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> MemberResponse:
    """改 user 角色. admin 不能改 owner role."""
    try:
        new_role = Role(req.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid role: {req.role}") from exc

    if new_role == Role.OWNER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "use /transfer-ownership to assign owner role",
        )

    membership_repo = MembershipRepository(conn)
    target = await membership_repo.get(user_id=member_user_id, org_id=org_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    acting_membership = user.org_by_id(org_id)
    acting_role = Role(acting_membership.role)  # type: ignore[union-attr]
    if acting_role == Role.ADMIN and target.role == Role.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin cannot change owner's role")

    if target.role == Role.OWNER and new_role != Role.OWNER:
        owner_count = await membership_repo.count_owners_in_org(org_id)
        if owner_count <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot demote the only owner")

    updated = await membership_repo.update_role(
        user_id=member_user_id, org_id=org_id, new_role=new_role
    )
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="member.role_changed",
        target_type="user",
        target_id=str(member_user_id),
        metadata={"from": target.role.value, "to": new_role.value},
    )

    user_repo = UserRepository(conn)
    target_user = await user_repo.get_by_id(member_user_id)

    return MemberResponse(
        user_id=target_user.id,  # type: ignore[union-attr]
        email=target_user.email,  # type: ignore[union-attr]
        display_name=target_user.display_name,  # type: ignore[union-attr]
        role=updated.role.value,  # type: ignore[union-attr]
        joined_at=updated.joined_at.isoformat(),  # type: ignore[union-attr]
    )


@router.post("/{org_id}/transfer-ownership", status_code=status.HTTP_204_NO_CONTENT)
async def transfer_ownership(
    org_id: UUID,
    req: TransferOwnershipRequest,
    user: UserContext = Depends(require_permission(Permission.TRANSFER_OWNERSHIP)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> None:
    """转让 ownership. 当前 owner 变 admin, 目标 user 变 owner."""
    if req.new_owner_user_id == user.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot transfer ownership to yourself")

    membership_repo = MembershipRepository(conn)
    target = await membership_repo.get(user_id=req.new_owner_user_id, org_id=org_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target user not in this org")

    async with conn.transaction():
        await membership_repo.update_role(user_id=user.user_id, org_id=org_id, new_role=Role.ADMIN)
        await membership_repo.update_role(
            user_id=req.new_owner_user_id, org_id=org_id, new_role=Role.OWNER
        )
