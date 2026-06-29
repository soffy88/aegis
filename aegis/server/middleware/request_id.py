"""Request-ID correlation + structured access-log middleware.

Pure ASGI (NOT BaseHTTPMiddleware): BaseHTTPMiddleware runs the downstream app
in a separate anyio task, so a ContextVar set there does NOT propagate to the
endpoint/its log records. A pure ASGI middleware sets the var in the same
context the downstream runs in, so request_id reliably appears in every log line
emitted while handling the request.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Current request's correlation id; "-" outside any request (startup, cron).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_access_log = logging.getLogger("aegis.access")


class RequestIdFilter(logging.Filter):
    """Inject the current request id onto every log record so the formatter can
    reference %(request_id)s without KeyError for out-of-request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIDMiddleware:
    """Tag each HTTP request with X-Request-ID and emit one access line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex[:12]
        token = request_id_var.set(incoming)
        start = time.monotonic()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                raw = list(message.get("headers") or [])
                raw.append((b"x-request-id", incoming.encode()))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur_ms = (time.monotonic() - start) * 1000.0
            _access_log.info(
                "access method=%s path=%s status=%d dur_ms=%.1f",
                scope.get("method", "?"),
                scope.get("path", "?"),
                status_code,
                dur_ms,
            )
            request_id_var.reset(token)


class JsonLogFormatter(logging.Formatter):
    """Minimal stdlib JSON log formatter (no extra dependency)."""

    def format(self, record: logging.LogRecord) -> str:
        import json  # noqa: PLC0415

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
