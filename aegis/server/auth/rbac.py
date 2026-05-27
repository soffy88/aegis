"""RBAC Permission 模型 + Depends 装饰器.

RFC v2 §4.2 18 操作 5 角色权限矩阵.

两种 Depends:
- require_min_role(role): 简单角色等级场景 (e.g. delete org 要 owner+)
- require_permission(perm): 精细权限场景 (e.g. operator 不能 install 但能 trigger autoheal)

operator 等级 < member, 但有 member 没有的独占行为 (跟 viewer 共享 dismiss alert).
单纯 role hierarchy 不够表达, 需要显式 permission.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, HTTPException, status

from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.models import ROLE_HIERARCHY, Role


class Permission(StrEnum):
    # Org 管理
    VIEW_ORG = "view_org"
    MODIFY_ORG = "modify_org"
    DELETE_ORG = "delete_org"
    TRANSFER_OWNERSHIP = "transfer_ownership"
    INVITE_USER = "invite_user"
    REMOVE_USER = "remove_user"
    CHANGE_USER_ROLE = "change_user_role"

    # Project 管理
    CREATE_PROJECT = "create_project"
    MODIFY_PROJECT = "modify_project"
    DELETE_PROJECT = "delete_project"
    VIEW_PROJECT = "view_project"

    # 应用层
    INSTALL_APP = "install_app"
    TRIGGER_AUTOHEAL = "trigger_autoheal"
    DISMISS_ALERT = "dismiss_alert"
    CONFIGURE_ALERT = "configure_alert"
    CONFIGURE_NOTIFY = "configure_notify"
    VIEW_EVENTS = "view_events"
    VIEW_AUDIT_LOG = "view_audit_log"  # M2 启用, M1 全权


# RFC v2.1 §4.2 静态权限映射
_VIEWER_PERMS = {
    Permission.VIEW_ORG,
    Permission.VIEW_PROJECT,
    Permission.VIEW_EVENTS,
    Permission.DISMISS_ALERT,  # v2.1: viewer 加 dismiss
}

_OPERATOR_PERMS = _VIEWER_PERMS | {
    Permission.TRIGGER_AUTOHEAL,  # 值班核心能力
}

_MEMBER_PERMS = _OPERATOR_PERMS | {
    Permission.CREATE_PROJECT,
    Permission.MODIFY_PROJECT,
    Permission.INSTALL_APP,
    Permission.CONFIGURE_ALERT,
    Permission.CONFIGURE_NOTIFY,
}

_ADMIN_PERMS = _MEMBER_PERMS | {
    Permission.MODIFY_ORG,
    Permission.INVITE_USER,
    Permission.REMOVE_USER,
    Permission.CHANGE_USER_ROLE,
    Permission.DELETE_PROJECT,
    Permission.VIEW_AUDIT_LOG,
}

_OWNER_PERMS = _ADMIN_PERMS | {
    Permission.DELETE_ORG,
    Permission.TRANSFER_OWNERSHIP,
}

PERMISSIONS_BY_ROLE: dict[Role, set[Permission]] = {
    Role.VIEWER: _VIEWER_PERMS,
    Role.OPERATOR: _OPERATOR_PERMS,
    Role.MEMBER: _MEMBER_PERMS,
    Role.ADMIN: _ADMIN_PERMS,
    Role.OWNER: _OWNER_PERMS,
}


def has_permission(role: Role | str, perm: Permission) -> bool:
    """静态检查 role 是否含 perm. 用于测试 + UI 灰化逻辑."""
    if isinstance(role, str):
        role = Role(role)
    return perm in PERMISSIONS_BY_ROLE[role]


# ====== FastAPI Depends ======


def require_min_role(min_role: Role) -> Callable[..., UserContext]:
    """要求 user 在 path 参数 org_id 上 ≥ min_role 等级.

    用于简单"等级"场景, e.g. delete_org 要 owner+.
    依赖 path 参数名 = 'org_id' (UUID).
    """

    async def dependency(
        org_id: UUID,
        user: UserContext = Depends(get_current_user),
    ) -> UserContext:
        membership = user.org_by_id(org_id)
        if not membership:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this org")

        user_role = Role(membership.role)
        if ROLE_HIERARCHY[user_role] < ROLE_HIERARCHY[min_role]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role {min_role.value} or higher",
            )
        return user

    return dependency


def require_permission(perm: Permission) -> Callable[..., UserContext]:
    """要求 user 在 path 参数 org_id 上有指定 perm.

    用于精细权限场景, e.g. operator 不能 install 但能 trigger autoheal.
    依赖 path 参数名 = 'org_id' (UUID).
    """

    async def dependency(
        org_id: UUID,
        user: UserContext = Depends(get_current_user),
    ) -> UserContext:
        membership = user.org_by_id(org_id)
        if not membership:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this org")

        user_role = Role(membership.role)
        if perm not in PERMISSIONS_BY_ROLE[user_role]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"missing permission: {perm.value}",
            )
        return user

    return dependency
