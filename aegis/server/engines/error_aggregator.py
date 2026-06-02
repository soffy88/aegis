"""ErrorAggregator — service-layer engine for error event → issue aggregation.

Workflow per event:
1. Compute real fingerprint via oprim.compute_event_fingerprint (no internal hashlib).
2. UPSERT error_issues by fingerprint (new or event_count++).
3. Backfill error_events: SET fingerprint = <real>, issue_id = <issue>.

Caller: ErrorIngestor (C3-3, injected via constructor).
"""

from __future__ import annotations

from typing import Any

from oprim import compute_event_fingerprint

from aegis.server.repositories.error_event_repository import ErrorEventRepository
from aegis.server.repositories.error_issue_repository import ErrorIssueRepository
from aegis.server.schemas.error_monitoring import ErrorEventResponse, ErrorIssueResponse


class ErrorAggregator:
    def __init__(
        self,
        *,
        event_repo: ErrorEventRepository,
        issue_repo: ErrorIssueRepository,
    ) -> None:
        self.event_repo = event_repo
        self.issue_repo = issue_repo

    async def aggregate_event(
        self,
        *,
        event: ErrorEventResponse,
        custom_fingerprint: list[str] | None = None,
    ) -> tuple[ErrorEventResponse, ErrorIssueResponse, bool]:
        """Aggregate one event: compute fingerprint → upsert issue → backfill event.

        Args:
            event: Event written by ErrorIngestor (placeholder fingerprint).
            custom_fingerprint: SDK-supplied grouping override from envelope
                payload['fingerprint']. When present, all other fields are ignored
                by oprim.compute_event_fingerprint.

        Returns:
            Tuple of (updated_event, issue, is_new).
            is_new=True means this fingerprint was seen for the first time.
        """
        top_func, top_file = self._top_frame(event.stacktrace)

        fingerprint = compute_event_fingerprint(
            exception_type=event.exception_type,
            exception_value=event.exception_value,
            top_frame_function=top_func,
            top_frame_filename=top_file,
            custom_fingerprint=custom_fingerprint,
        )

        issue, is_new = await self.issue_repo.upsert_by_fingerprint(
            org_id=event.org_id,
            project_id=event.project_id,
            fingerprint=fingerprint,
            exception_type=event.exception_type,
            exception_value=event.exception_value,
            release_name=event.release_name,
        )

        await self.event_repo.update_fingerprint_and_issue(
            event_id=event.event_id,
            fingerprint=fingerprint,
            issue_id=issue.issue_id,
        )

        updated_event = event.model_copy(
            update={"fingerprint": fingerprint, "issue_id": issue.issue_id}
        )
        return (updated_event, issue, is_new)

    @staticmethod
    def _top_frame(
        stacktrace: dict[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        """Extract (function, filename) of the innermost frame from a Sentry stacktrace."""
        if not stacktrace:
            return (None, None)
        frames = stacktrace.get("frames", [])
        if not frames:
            return (None, None)
        top = frames[-1]
        return (top.get("function"), top.get("filename"))
