"""C1-3 RBAC model unit tests — no DB required."""

from __future__ import annotations

from aegis.server.auth.rbac import PERMISSIONS_BY_ROLE, Permission, has_permission
from aegis.server.models import ROLE_HIERARCHY, Role


class TestRbacModel:
    def test_role_hierarchy_operator_less_than_member(self) -> None:
        assert ROLE_HIERARCHY[Role.OPERATOR] < ROLE_HIERARCHY[Role.MEMBER]

    def test_permission_owner_has_all(self) -> None:
        owner_perms = PERMISSIONS_BY_ROLE[Role.OWNER]
        for perm in Permission:
            assert perm in owner_perms, f"owner missing {perm}"

    def test_permission_admin_lacks_delete_org(self) -> None:
        assert Permission.DELETE_ORG not in PERMISSIONS_BY_ROLE[Role.ADMIN]
        assert Permission.TRANSFER_OWNERSHIP not in PERMISSIONS_BY_ROLE[Role.ADMIN]

    def test_permission_member_can_install_app(self) -> None:
        assert Permission.INSTALL_APP in PERMISSIONS_BY_ROLE[Role.MEMBER]

    def test_permission_operator_can_trigger_autoheal_not_install(self) -> None:
        operator_perms = PERMISSIONS_BY_ROLE[Role.OPERATOR]
        assert Permission.TRIGGER_AUTOHEAL in operator_perms
        assert Permission.INSTALL_APP not in operator_perms

    def test_permission_viewer_can_dismiss_alert(self) -> None:
        assert Permission.DISMISS_ALERT in PERMISSIONS_BY_ROLE[Role.VIEWER]

    def test_permission_viewer_cannot_install(self) -> None:
        viewer_perms = PERMISSIONS_BY_ROLE[Role.VIEWER]
        assert Permission.INSTALL_APP not in viewer_perms
        assert Permission.INVITE_USER not in viewer_perms
        assert Permission.MODIFY_ORG not in viewer_perms

    def test_has_permission_with_string_role(self) -> None:
        assert has_permission("owner", Permission.DELETE_ORG) is True
        assert has_permission("viewer", Permission.DELETE_ORG) is False

    def test_has_permission_all_permissions_mapped(self) -> None:
        """Every Permission value must appear in at least OWNER's set."""
        owner_perms = PERMISSIONS_BY_ROLE[Role.OWNER]
        unmapped = [p for p in Permission if p not in owner_perms]
        assert unmapped == [], f"unmapped permissions: {unmapped}"

    def test_member_lacks_admin_perms(self) -> None:
        member_perms = PERMISSIONS_BY_ROLE[Role.MEMBER]
        assert Permission.INVITE_USER not in member_perms
        assert Permission.REMOVE_USER not in member_perms
        assert Permission.CHANGE_USER_ROLE not in member_perms
        assert Permission.DELETE_PROJECT not in member_perms
