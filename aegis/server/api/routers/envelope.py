"""Sentry envelope receiver endpoint — C3-6.

POST /api/{project_id}/envelope/
Headers: X-Sentry-Auth: Sentry sentry_version=7, sentry_key=<public_key>, ...
Body: Sentry envelope bytes (newline-separated JSON)

Pipeline: sentry_auth → DB auth → ErrorIngestor → ErrorAggregator → ErrorAlerter
"""

from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from aegis.server.api.deps import get_db_conn
from aegis.server.engines.error_aggregator import ErrorAggregator
from aegis.server.engines.error_alerter import ErrorAlerter
from aegis.server.engines.error_ingestor import ErrorIngestor
from aegis.server.engines.webhook_dispatcher import WebhookDispatcher
from aegis.server.lib.sentry_auth import SentryAuthError, parse_sentry_auth_header
from aegis.server.repositories.error_event_repository import ErrorEventRepository
from aegis.server.repositories.error_issue_repository import ErrorIssueRepository
from aegis.server.repositories.project_repo import ProjectRepository
from aegis.server.repositories.webhook_delivery_repository import (
    WebhookDeliveryQueueRepository,
)
from aegis.server.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)

router = APIRouter(prefix="/api", tags=["envelope"])


@router.post("/{project_id}/envelope/")
async def receive_envelope(
    project_id: uuid.UUID,
    request: Request,
    x_sentry_auth: str = Header(..., alias="X-Sentry-Auth"),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict:
    """Receive a Sentry envelope from an SDK.

    Public endpoint — no JWT required. Uses DSN public_key for project auth.

    Error responses:
    - 400: malformed X-Sentry-Auth or empty body
    - 403: (project_id, public_key) pair not found in DB
    """
    # 1. Parse X-Sentry-Auth header
    try:
        auth = parse_sentry_auth_header(x_sentry_auth)
    except SentryAuthError as exc:
        raise HTTPException(status_code=400, detail=f"invalid X-Sentry-Auth: {exc}") from exc

    # 2. Verify (project_id, public_key) → get Project (contains org_id)
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id_and_public_key(
        project_id=project_id, public_key=auth.public_key
    )
    if project is None:
        raise HTTPException(status_code=403, detail="invalid project_id or public_key")

    # 3. Read envelope body
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty envelope body")

    # 4. Wire Ingest → Aggregate → Alert pipeline
    event_repo = ErrorEventRepository(conn)
    issue_repo = ErrorIssueRepository(conn)
    sub_repo = WebhookSubscriptionRepository(conn)
    delivery_repo = WebhookDeliveryQueueRepository(conn)

    webhook_dispatcher = WebhookDispatcher(sub_repo=sub_repo, delivery_repo=delivery_repo)
    aggregator = ErrorAggregator(event_repo=event_repo, issue_repo=issue_repo)
    alerter = ErrorAlerter(webhook_dispatcher=webhook_dispatcher)
    ingestor = ErrorIngestor(event_repo=event_repo, aggregator=aggregator, alerter=alerter)

    # 5. Process envelope
    events = await ingestor.ingest_envelope(
        org_id=project.org_id,
        project_id=project_id,
        envelope_bytes=body,
        conn=conn,
    )

    # 6. Return Sentry-compatible response (SDK expects {"id": "<hex>"})
    if not events:
        return {"id": None}
    return {"id": events[0].event_id.hex}
