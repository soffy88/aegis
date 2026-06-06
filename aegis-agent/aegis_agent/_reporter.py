"""HTTP reporter — POSTs metric batches to Aegis backend /api/v1/metrics/ingest."""

from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


class MetricsReporter:
    """POST metric points to Aegis backend ingest endpoint.

    Args:
        backend_url: Base URL of the Aegis backend (e.g. 'http://aegis:8080').
        agent_token: Bearer token sent as Authorization header. Empty = no auth.
        hostname: Override reported hostname. Defaults to socket.gethostname().
        timeout: HTTP request timeout seconds.
    """

    def __init__(
        self,
        backend_url: str,
        agent_token: str = "",
        hostname: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._url = backend_url.rstrip("/") + "/api/v1/metrics/ingest"
        self._token = agent_token
        self._hostname = hostname or socket.gethostname()
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def report(self, metrics: list[dict[str, Any]]) -> bool:
        """POST metrics to backend. Returns True on success, False on failure."""
        if not metrics:
            return True
        payload = {
            "hostname": self._hostname,
            "collected_at": datetime.now(tz=UTC).isoformat(),
            "metrics": metrics,
        }
        try:
            resp = httpx.post(
                self._url,
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
            if resp.status_code == 202:
                accepted = resp.json().get("accepted", len(metrics))
                log.info(
                    "metrics_reported accepted=%d hostname=%s",
                    accepted,
                    self._hostname,
                )
                return True
            log.warning(
                "metrics_report_failed status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        except httpx.RequestError as exc:
            log.warning("metrics_report_error: %s", exc)
            return False
