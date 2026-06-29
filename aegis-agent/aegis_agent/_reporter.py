"""HTTP reporter — POSTs metric batches to Aegis backend /api/v1/metrics/ingest."""

from __future__ import annotations

import logging
import socket
import time
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
        max_retries: int = 2,
        backoff_base: float = 0.5,
    ) -> None:
        self._url = backend_url.rstrip("/") + "/api/v1/metrics/ingest"
        self._token = agent_token
        self._hostname = hostname or socket.gethostname()
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def report(self, metrics: list[dict[str, Any]]) -> bool:
        """POST metrics to backend with bounded retry. Returns True on success.

        Retries transient failures (connection errors, 429, 5xx) with exponential
        backoff. Permanent failures (other 4xx — auth/validation) are not retried.
        """
        if not metrics:
            return True
        payload = {
            "hostname": self._hostname,
            "collected_at": datetime.now(tz=UTC).isoformat(),
            "metrics": metrics,
        }

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            retryable = False
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
                        "metrics_reported accepted=%d hostname=%s", accepted, self._hostname
                    )
                    return True
                # 429 / 5xx are transient; other 4xx are permanent (no retry).
                retryable = resp.status_code == 429 or resp.status_code >= 500
                log.warning(
                    "metrics_report_failed status=%d body=%s retryable=%s",
                    resp.status_code,
                    resp.text[:200],
                    retryable,
                )
                if not retryable:
                    return False
            except httpx.RequestError as exc:
                retryable = True
                log.warning("metrics_report_error: %s", exc)

            if retryable and attempt < attempts - 1:
                time.sleep(self._backoff_base * (2**attempt))

        log.warning("metrics_report_giving_up after=%d attempts", attempts)
        return False
