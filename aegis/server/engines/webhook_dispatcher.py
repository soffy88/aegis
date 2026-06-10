"""WebhookDispatcher — 服务层引擎 (C2-5).

负责:
1. enqueue_event: 根据 event_type 找订阅 sub, 入队 delivery
2. deliver_batch: claim pending deliveries, 调主库 oprim.http_post_webhook
3. retry 逻辑 + dead letter

主库依赖:
- oprim.http_post_webhook: 单次 HTTP POST (sync)
- obase.webhook.sign_payload: HMAC-SHA256 签名

不做:
- 自己实现 HTTP / HMAC (V7 范式合规自检 0 命中)
- 跨 worker 协调 (M1 单进程, FOR UPDATE SKIP LOCKED 保安全)
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from obase.webhook import sign_payload
from oprim import http_post_webhook

from aegis.server.repositories.webhook_delivery_repository import WebhookDeliveryQueueRepository
from aegis.server.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)

# Only env vars with this prefix may be used for webhook signing keys.
# Prevents tenant-controlled input from reading arbitrary server secrets.
_WEBHOOK_SECRET_ENV_PREFIX = "AEGIS_WEBHOOK_SECRET_"


def _resolve_secret(secret_encrypted: str | None) -> str | None:
    """Resolve secret_encrypted field to a plain secret string.

    - 'env:AEGIS_WEBHOOK_SECRET_*' → os.environ[var]  (allowlisted prefix only)
    - 'plain:xxx'                  → xxx  (dev/testing only; warn in prod)
    - None or unrecognised         → None (no signing)
    """
    if not secret_encrypted:
        return None
    if secret_encrypted.startswith("env:"):
        var_name = secret_encrypted[4:]
        if not var_name.startswith(_WEBHOOK_SECRET_ENV_PREFIX):
            return None  # reject env var names outside the dedicated allowlist prefix
        return os.environ.get(var_name)
    if secret_encrypted.startswith("plain:"):
        env = os.environ.get("AEGIS_ENV", "dev")
        if env != "dev":
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning(
                "webhook_secret_plaintext: secret stored as 'plain:' in non-dev env (%s). "
                "Use 'env:AEGIS_WEBHOOK_SECRET_<NAME>' for production.",
                env,
            )
        return secret_encrypted[6:]
    return None


class WebhookDispatcher:
    def __init__(
        self,
        *,
        sub_repo: WebhookSubscriptionRepository,
        delivery_repo: WebhookDeliveryQueueRepository,
    ) -> None:
        self.sub_repo = sub_repo
        self.delivery_repo = delivery_repo

    async def enqueue_event(
        self,
        *,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Enqueue delivery for every enabled sub subscribed to event_type.

        Returns count of rows enqueued.
        """
        subs = await self.sub_repo.list_by_org(org_id=org_id, enabled_only=True)
        enqueued = 0
        for sub in subs:
            if event_type in sub.event_types:
                await self.delivery_repo.enqueue(
                    sub_id=sub.sub_id,
                    org_id=org_id,
                    event_type=event_type,
                    payload=payload,
                    max_attempts=sub.retry_count + 1,
                )
                enqueued += 1
        return enqueued

    async def deliver_batch(self, *, batch_size: int = 10) -> dict[str, int]:
        """Claim and deliver pending deliveries. Returns stats."""
        deliveries = await self.delivery_repo.claim_next_batch(batch_size=batch_size)
        stats: dict[str, int] = {"succeeded": 0, "failed_retry": 0, "dead_letter": 0}

        for d in deliveries:
            sub = await self.sub_repo.get(sub_id=d.sub_id, org_id=d.org_id)
            if not sub or not sub.enabled:
                await self.delivery_repo.mark_dead_letter(
                    delivery_id=d.delivery_id,
                    status_code=None,
                    error="subscription disabled or deleted",
                )
                stats["dead_letter"] += 1
                continue

            secret = _resolve_secret(sub.secret_encrypted)
            signature = sign_payload(payload=d.payload, secret=secret) if secret else None

            try:
                result = http_post_webhook(
                    url=sub.url,
                    payload=d.payload,
                    timeout_sec=10.0,
                    signature=signature,
                    user_agent="Aegis-Webhook/1.0",
                )
            except Exception as exc:
                attempts_remaining = d.max_attempts - (d.attempt_no + 1)
                if attempts_remaining <= 0:
                    await self.delivery_repo.mark_dead_letter(
                        delivery_id=d.delivery_id,
                        status_code=None,
                        error=str(exc),
                    )
                    stats["dead_letter"] += 1
                else:
                    backoff = sub.retry_backoff_seconds[
                        min(d.attempt_no, len(sub.retry_backoff_seconds) - 1)
                    ]
                    await self.delivery_repo.mark_failed_for_retry(
                        delivery_id=d.delivery_id,
                        status_code=None,
                        error=str(exc),
                        backoff_seconds=backoff,
                    )
                    stats["failed_retry"] += 1
                continue

            if result.success:
                await self.delivery_repo.mark_succeeded(
                    delivery_id=d.delivery_id,
                    status_code=result.status_code,
                )
                stats["succeeded"] += 1
            elif result.status_code in (408, 429) or result.status_code >= 500:
                attempts_remaining = d.max_attempts - (d.attempt_no + 1)
                if attempts_remaining <= 0:
                    await self.delivery_repo.mark_dead_letter(
                        delivery_id=d.delivery_id,
                        status_code=result.status_code,
                        error=result.error or f"HTTP {result.status_code}",
                    )
                    stats["dead_letter"] += 1
                else:
                    backoff = sub.retry_backoff_seconds[
                        min(d.attempt_no, len(sub.retry_backoff_seconds) - 1)
                    ]
                    await self.delivery_repo.mark_failed_for_retry(
                        delivery_id=d.delivery_id,
                        status_code=result.status_code,
                        error=result.error or f"HTTP {result.status_code}",
                        backoff_seconds=backoff,
                    )
                    stats["failed_retry"] += 1
            else:
                # 4xx (excluding 408/429) — permanent client error → dead letter
                await self.delivery_repo.mark_dead_letter(
                    delivery_id=d.delivery_id,
                    status_code=result.status_code,
                    error=result.error or f"HTTP {result.status_code} (permanent)",
                )
                stats["dead_letter"] += 1

        return stats
