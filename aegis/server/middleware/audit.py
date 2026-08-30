"""Audit Middleware — 自动记录所有写操作到 audit_log.

AEGIS_DESIGN v1.1.0 §6.3
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from aegis.server.persistence import record_audit, get_pool

log = logging.getLogger(__name__)

# 定义需要审计的写操作路径模式
AUDIT_WRITE_PATHS = {
    # Apps
    "/api/v1/apps": ("POST", "app.installed"),
    "/api/v1/apps/{app_id}": ("PUT", "app.updated"),
    "/api/v1/apps/{app_id}/upgrade": ("POST", "app.upgraded"),
    "/api/v1/apps/{app_id}/rollback": ("POST", "app.rolled_back"),
    "/api/v1/apps/{app_id}/start": ("POST", "app.started"),
    "/api/v1/apps/{app_id}/stop": ("POST", "app.stopped"),
    "/api/v1/apps/{app_id}/restart": ("POST", "app.restarted"),
    "/api/v1/apps/{app_id}": ("DELETE", "app.uninstalled"),

    # Alert rules
    "/api/v1/alerts/rules": ("POST", "alert_rule.created"),
    "/api/v1/alerts/rules/{rule_id}": ("PUT", "alert_rule.updated"),
    "/api/v1/alerts/rules/{rule_id}": ("DELETE", "alert_rule.deleted"),

    # Secrets
    "/api/v1/secrets": ("POST", "secret.created"),
    "/api/v1/secrets/{secret_id}": ("PUT", "secret.rotated"),
    "/api/v1/secrets/{secret_id}": ("DELETE", "secret.deleted"),

    # Config
    "/api/v1/config/dryrun": ("POST", "config.dryrun"),
    "/api/v1/config/apply": ("POST", "config.applied"),
    "/api/v1/config/rollback": ("POST", "config.rolled_back"),

    # Users/Invites
    "/api/v1/users/invite": ("POST", "user.invited"),
    "/api/v1/users/{user_id}": ("PUT", "user.updated"),
    "/api/v1/users/{user_id}": ("DELETE", "user.removed"),
    "/api/v1/users/{user_id}/role": ("PUT", "user.role_changed"),

    # Projects
    "/api/v1/projects": ("POST", "project.created"),
    "/api/v1/projects/{project_id}": ("PUT", "project.updated"),
    "/api/v1/projects/{project_id}": ("DELETE", "project.deleted"),

    # Orgs
    "/api/v1/orgs": ("POST", "org.created"),
    "/api/v1/orgs/{org_id}": ("PUT", "org.updated"),
    "/api/v1/orgs/{org_id}": ("DELETE", "org.deleted"),
}


class AuditMiddleware(BaseHTTPMiddleware):
    """自动记录写操作的审计日志"""

    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 只审计写操作
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        # 提取 org_id (从 JWT 或 path 参数)
        org_id = await self._extract_org_id(request)
        if not org_id:
            return await call_next(request)

        # 提取用户信息
        user = getattr(request.state, "user", None)
        actor_user_id = user.user_id if user else None

        # 执行请求
        response = await call_next(request)

        # 仅记录成功/部分失败的写操作
        if response.status_code < 500:
            await self._record_audit(
                request=request,
                response=response,
                org_id=org_id,
                actor_user_id=actor_user_id,
            )

        return response

    async def _extract_org_id(self, request: Request) -> str | None:
        """从路径参数或 JWT 提取 org_id"""
        # 1. path 参数
        if "org_id" in request.path_params:
            return str(request.path_params["org_id"])

        # 2. JWT claims (如果可用)
        user = getattr(request.state, "user", None)
        if user and user.orgs:
            # 取第一个 org (contextual)
            return str(user.orgs[0].org_id)

        return None

    async def _record_audit(
        self,
        request: Request,
        response: Response,
        org_id: str,
        actor_user_id: str | None,
    ) -> None:
        """记录审计日志 (best-effort)"""
        try:
            # 确定 action type
            action = self._resolve_action(request)
            if not action:
                return

            # 提取 target_type/target_id
            target_type, target_id = self._extract_target(request)

            # 提取 metadata (请求体 + 响应状态)
            metadata = self._extract_metadata(request, response)

            # 获取 IP + User-Agent
            ip = self._get_client_ip(request)
            user_agent = request.headers.get("user-agent", "")

            # 写入审计日志 (异步不阻塞)
            pool = get_pool()
            async with pool.acquire() as conn:
                await record_audit(
                    conn,
                    org_id=org_id,
                    action=action,
                    actor_user_id=actor_user_id,
                    target_type=target_type,
                    target_id=target_id,
                    metadata=metadata,
                    ip=ip,
                    user_agent=user_agent,
                )
        except Exception as e:
            log.warning("audit_middleware_failed: %s", e)

    def _resolve_action(self, request: Request) -> str | None:
        """根据路径和方法解析 action"""
        path = request.url.path
        method = request.method

        # 精确匹配
        for pattern, (expected_method, action) in AUDIT_WRITE_PATHS.items():
            if method == expected_method and self._match_path(path, pattern):
                return action

        # 通配符匹配: /api/v1/...
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            if path.startswith("/api/v1/"):
                # 从路径推断
                parts = path.strip("/").split("/")
                if len(parts) >= 3:
                    resource = parts[2]  # /api/v1/{resource}/...
                    action_map = {
                        "POST": f"{resource}.created",
                        "PUT": f"{resource}.updated",
                        "DELETE": f"{resource}.deleted",
                        "PATCH": f"{resource}.patched",
                    }
                    return action_map.get(method, f"{resource}.{method.lower()}")

        return None

    def _match_path(self, path: str, pattern: str) -> bool:
        """简单路径匹配：支持 {var} 占位符"""
        path_parts = path.strip("/").split("/")
        pattern_parts = pattern.strip("/").split("/")

        if len(path_parts) != len(pattern_parts):
            return False

        for p, pt in zip(path_parts, pattern_parts):
            if pt.startswith("{") and pt.endswith("}"):
                continue
            if p != pt:
                return False
        return True

    def _extract_target(self, request: Request) -> tuple[str | None, str | None]:
        """从路径提取 target_type, target_id"""
        path = request.url.path
        parts = path.strip("/").split("/")

        if len(parts) >= 3:
            resource = parts[2]
            # 查找 ID 参数
            for i, part in enumerate(parts):
                if i > 2 and part and not part.startswith("_"):
                    # 可能是 ID
                    return resource, part

        return None, None

    def _extract_metadata(self, request: Request, response: Response) -> dict:
        """提取请求/响应的结构化 metadata"""
        metadata = {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
        }

        # 尝试读取请求体 (小心内存)
        if hasattr(request, "_body") and request._body:
            try:
                body = json.loads(request._body)
                if isinstance(body, dict):
                    # 过滤敏感字段
                    filtered = {k: v for k, v in body.items()
                               if k not in ("password", "token", "secret", "key")}
                    metadata["request"] = filtered
            except Exception:
                pass

        return metadata

    def _get_client_ip(self, request: Request) -> str | None:
        """提取客户端 IP (考虑代理)"""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        if request.client:
            return request.client.host
        return None


def setup_audit_middleware(app) -> None:
    """在 FastAPI app 中注册审计中间件"""
    app.add_middleware(AuditMiddleware)