"""ErrorIngestor — service-layer engine for Sentry envelope ingestion.

M1 scope:
- Parse envelope bytes → list of event payloads
- Write each event to error_events (fingerprint is a placeholder)
- If aggregator is injected (C3-4): compute real fingerprint + upsert issue

M1 fingerprint placeholder: "envelope-temp-<uuid4>" — avoids NULL constraint
and makes rows identifiable before aggregation runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aegis.server.lib.sentry_envelope import (
    SentryEnvelopeParseError,
    extract_exception,
    parse_envelope,
)
from aegis.server.repositories.error_event_repository import ErrorEventRepository
from aegis.server.schemas.error_monitoring import ErrorEventCreate, ErrorEventResponse

if TYPE_CHECKING:
    from aegis.server.engines.error_aggregator import ErrorAggregator


class ErrorIngestor:
    def __init__(
        self,
        *,
        event_repo: ErrorEventRepository,
        aggregator: ErrorAggregator | None = None,
    ) -> None:
        self.event_repo = event_repo
        self.aggregator = aggregator

    async def ingest_envelope(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        envelope_bytes: bytes,
    ) -> list[ErrorEventResponse]:
        """Parse envelope and persist all event items to error_events.

        Non-event items (transaction / session / replay) are skipped.
        A single item that fails to parse does not abort the rest.

        Returns list of inserted ErrorEventResponse (may be empty).
        """
        event_payloads = parse_envelope(envelope_bytes)

        results: list[ErrorEventResponse] = []
        for payload in event_payloads:
            try:
                event = await self._ingest_single(
                    org_id=org_id,
                    project_id=project_id,
                    payload=payload,
                )
                # C3-4: aggregate if aggregator injected (backward-compatible)
                if self.aggregator is not None:
                    custom_fp = payload.get("fingerprint")
                    event, _issue, _is_new = await self.aggregator.aggregate_event(
                        event=event,
                        custom_fingerprint=custom_fp if isinstance(custom_fp, list) else None,
                    )
                results.append(event)
            except SentryEnvelopeParseError:
                # Single-event parse failure: skip, do not abort batch.
                # M2: add dead-letter queue here.
                continue

        return results

    async def _ingest_single(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> ErrorEventResponse:
        exc_type, exc_value = extract_exception(payload)
        sdk = payload.get("sdk") or {}
        data = ErrorEventCreate(
            org_id=org_id,
            project_id=project_id,
            # M1 placeholder — C3-4 ErrorAggregator overwrites with real fingerprint
            fingerprint=f"envelope-temp-{uuid.uuid4()}",
            ts=self._parse_ts(payload.get("timestamp")),
            exception_type=exc_type,
            exception_value=exc_value,
            level=payload.get("level", "error"),
            environment=payload.get("environment", "prod"),
            server_name=payload.get("server_name"),
            release_name=payload.get("release"),
            stacktrace=self._extract_stacktrace(payload),
            breadcrumbs=(payload.get("breadcrumbs") or {}).get("values"),
            user_context=payload.get("user"),
            tags=payload.get("tags"),
            extra=payload.get("extra"),
            sdk_name=sdk.get("name"),
            sdk_version=sdk.get("version"),
            platform=payload.get("platform"),
        )
        return await self.event_repo.insert(data=data)

    @staticmethod
    def _parse_ts(ts: Any) -> datetime | None:
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=UTC)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_stacktrace(payload: dict[str, Any]) -> dict[str, Any] | None:
        values = payload.get("exception", {}).get("values", [])
        if not values:
            return None
        return values[0].get("stacktrace")
