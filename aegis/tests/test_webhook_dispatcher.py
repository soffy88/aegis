"""Tests for WebhookDispatcher — C2-5. Pure unit tests (mocked repos + oprim)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aegis.server.schemas.webhook import (
    WebhookDeliveryResponse,
    WebhookSubscriptionResponse,
)

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SUB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DELIVERY_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_USER = uuid.UUID("44444444-4444-4444-4444-444444444444")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _sub(**kwargs: Any) -> WebhookSubscriptionResponse:
    base: dict[str, Any] = dict(
        sub_id=_SUB_ID,
        org_id=_ORG,
        name="my-hook",
        url="https://example.com/webhook",
        secret_encrypted=None,
        event_types=["alert.fired"],
        retry_count=3,
        retry_backoff_seconds=[5, 15, 45],
        enabled=True,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    base.update(kwargs)
    return WebhookSubscriptionResponse.model_validate(base)


def _delivery(**kwargs: Any) -> WebhookDeliveryResponse:
    base: dict[str, Any] = dict(
        delivery_id=_DELIVERY_ID,
        sub_id=_SUB_ID,
        org_id=_ORG,
        event_type="alert.fired",
        payload={"rule_id": "abc"},
        attempt_no=0,
        max_attempts=4,
        next_attempt_at=_NOW,
        last_attempt_at=None,
        last_status_code=None,
        last_error=None,
        state="in_flight",
        created_at=_NOW,
        succeeded_at=None,
    )
    base.update(kwargs)
    return WebhookDeliveryResponse.model_validate(base)


def _make_dispatcher(
    subs: list[WebhookSubscriptionResponse] | None = None,
    claim_return: list[WebhookDeliveryResponse] | None = None,
) -> Any:
    from aegis.server.engines.webhook_dispatcher import WebhookDispatcher

    sub_repo = MagicMock()
    sub_repo.list_by_org = AsyncMock(return_value=subs or [])
    sub_repo.get = AsyncMock(return_value=(_sub() if subs is None else (subs[0] if subs else None)))

    delivery_repo = MagicMock()
    delivery_repo.claim_next_batch = AsyncMock(return_value=claim_return or [])
    delivery_repo.enqueue = AsyncMock()
    delivery_repo.mark_succeeded = AsyncMock()
    delivery_repo.mark_failed_for_retry = AsyncMock()
    delivery_repo.mark_dead_letter = AsyncMock()

    return WebhookDispatcher(sub_repo=sub_repo, delivery_repo=delivery_repo)


class TestEnqueueEvent:
    async def test_enqueue_event_filters_by_event_types(self) -> None:
        """Only subs subscribed to the event type get enqueued."""
        subs = [
            _sub(sub_id=uuid.uuid4(), event_types=["alert.fired"]),
            _sub(sub_id=uuid.uuid4(), event_types=["autoheal.completed"]),
        ]
        dispatcher = _make_dispatcher(subs=subs)
        count = await dispatcher.enqueue_event(
            org_id=_ORG, event_type="alert.fired", payload={"x": 1}
        )
        assert count == 1
        dispatcher.delivery_repo.enqueue.assert_awaited_once()

    async def test_enqueue_event_skips_disabled_subs(self) -> None:
        """Disabled subs are excluded by list_by_org(enabled_only=True)."""
        # list_by_org returns empty (simulating enabled_only filtering at repo level)
        dispatcher = _make_dispatcher(subs=[])
        count = await dispatcher.enqueue_event(org_id=_ORG, event_type="alert.fired", payload={})
        assert count == 0
        dispatcher.delivery_repo.enqueue.assert_not_awaited()

    async def test_enqueue_event_multiple_matching_subs(self) -> None:
        """All matching subs get their own delivery row."""
        subs = [
            _sub(sub_id=uuid.uuid4(), event_types=["alert.fired"]),
            _sub(sub_id=uuid.uuid4(), event_types=["alert.fired", "autoheal.completed"]),
        ]
        dispatcher = _make_dispatcher(subs=subs)
        count = await dispatcher.enqueue_event(org_id=_ORG, event_type="alert.fired", payload={})
        assert count == 2
        assert dispatcher.delivery_repo.enqueue.await_count == 2

    async def test_max_attempts_is_retry_count_plus_one(self) -> None:
        """max_attempts = retry_count + 1 (initial attempt + retries)."""
        subs = [_sub(retry_count=3, event_types=["alert.fired"])]
        dispatcher = _make_dispatcher(subs=subs)
        await dispatcher.enqueue_event(org_id=_ORG, event_type="alert.fired", payload={})
        call_kwargs = dispatcher.delivery_repo.enqueue.call_args.kwargs
        assert call_kwargs["max_attempts"] == 4  # 3 + 1


class TestDeliverBatch:
    async def test_deliver_succeeds_calls_oprim_http_post_webhook(self) -> None:
        from oprim import WebhookResult

        delivery = _delivery()
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=True, status_code=200, elapsed_ms=50.0, response_body="ok", error=None
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["succeeded"] == 1
        assert stats["dead_letter"] == 0
        dispatcher.delivery_repo.mark_succeeded.assert_awaited_once()

    async def test_deliver_4xx_marks_dead_letter(self) -> None:
        from oprim import WebhookResult

        delivery = _delivery()
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=False, status_code=404, elapsed_ms=30.0, response_body="", error="not found"
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["dead_letter"] == 1
        dispatcher.delivery_repo.mark_dead_letter.assert_awaited_once()

    async def test_deliver_5xx_retries_with_backoff(self) -> None:
        from oprim import WebhookResult

        delivery = _delivery(attempt_no=0, max_attempts=4)
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=False, status_code=500, elapsed_ms=100.0, response_body="", error="server err"
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["failed_retry"] == 1
        dispatcher.delivery_repo.mark_failed_for_retry.assert_awaited_once()

    async def test_deliver_429_retries(self) -> None:
        from oprim import WebhookResult

        delivery = _delivery(attempt_no=0, max_attempts=4)
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=False, status_code=429, elapsed_ms=50.0, response_body="", error="rate limited"
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["failed_retry"] == 1

    async def test_deliver_408_retries(self) -> None:
        from oprim import WebhookResult

        delivery = _delivery(attempt_no=0, max_attempts=4)
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=False, status_code=408, elapsed_ms=50.0, response_body="", error="timeout"
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["failed_retry"] == 1

    async def test_deliver_oprim_exception_retries(self) -> None:
        """Network-level exception → retry if attempts remaining."""
        delivery = _delivery(attempt_no=0, max_attempts=4)
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook",
            side_effect=ConnectionError("refused"),
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["failed_retry"] == 1
        dispatcher.delivery_repo.mark_failed_for_retry.assert_awaited_once()

    async def test_deliver_exceeds_max_attempts_marks_dead_letter(self) -> None:
        """When attempt_no + 1 >= max_attempts, goes straight to dead letter."""
        from oprim import WebhookResult

        # attempt_no=3, max_attempts=4 → attempts_remaining = 4 - (3+1) = 0
        delivery = _delivery(attempt_no=3, max_attempts=4)
        dispatcher = _make_dispatcher(subs=[_sub()], claim_return=[delivery])
        result = WebhookResult(
            success=False, status_code=500, elapsed_ms=50.0, response_body="", error="err"
        )
        with patch(
            "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
        ):
            stats = await dispatcher.deliver_batch()
        assert stats["dead_letter"] == 1
        dispatcher.delivery_repo.mark_dead_letter.assert_awaited_once()

    async def test_signature_included_when_secret_set(self) -> None:
        """When secret_encrypted is set, sign_payload is called and signature passed."""
        from oprim import WebhookResult

        sub_with_secret = _sub(secret_encrypted="plain:mysecret")
        delivery = _delivery()
        dispatcher = _make_dispatcher(subs=[sub_with_secret], claim_return=[delivery])
        dispatcher.sub_repo.get = AsyncMock(return_value=sub_with_secret)
        result = WebhookResult(
            success=True, status_code=200, elapsed_ms=10.0, response_body="ok", error=None
        )
        with (
            patch(
                "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
            ) as mock_post,
            patch(
                "aegis.server.engines.webhook_dispatcher.sign_payload", return_value="sig123"
            ) as mock_sign,
        ):
            await dispatcher.deliver_batch()
        mock_sign.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["signature"] == "sig123"

    async def test_signature_omitted_when_no_secret(self) -> None:
        """When secret_encrypted is None, sign_payload is NOT called."""
        from oprim import WebhookResult

        sub_no_secret = _sub(secret_encrypted=None)
        delivery = _delivery()
        dispatcher = _make_dispatcher(subs=[sub_no_secret], claim_return=[delivery])
        dispatcher.sub_repo.get = AsyncMock(return_value=sub_no_secret)
        result = WebhookResult(
            success=True, status_code=200, elapsed_ms=10.0, response_body="ok", error=None
        )
        with (
            patch(
                "aegis.server.engines.webhook_dispatcher.http_post_webhook", return_value=result
            ) as mock_post,
            patch("aegis.server.engines.webhook_dispatcher.sign_payload") as mock_sign,
        ):
            await dispatcher.deliver_batch()
        mock_sign.assert_not_called()
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["signature"] is None

    async def test_disabled_sub_marks_dead_letter(self) -> None:
        """If subscription is disabled when delivery is processed, mark dead letter."""
        delivery = _delivery()
        dispatcher = _make_dispatcher(subs=[], claim_return=[delivery])
        # sub_repo.get returns None (deleted/disabled)
        dispatcher.sub_repo.get = AsyncMock(return_value=None)

        with patch("aegis.server.engines.webhook_dispatcher.http_post_webhook") as mock_post:
            stats = await dispatcher.deliver_batch()
        mock_post.assert_not_called()
        assert stats["dead_letter"] == 1
