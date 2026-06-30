"""Audit #16: gate decisions must emit release.approved/rejected webhooks.

The decide endpoint previously built ReleaseGateService without a dispatcher, so
no event was ever enqueued. Verify the router now constructs a real dispatcher.
"""

from __future__ import annotations

from unittest import mock

from aegis.server.api.routers.release_gates import _build_webhook_dispatcher
from aegis.server.engines.webhook_dispatcher import WebhookDispatcher


def test_build_webhook_dispatcher_returns_real_dispatcher():
    d = _build_webhook_dispatcher(mock.AsyncMock())
    assert isinstance(d, WebhookDispatcher)
    assert d.sub_repo is not None and d.delivery_repo is not None
