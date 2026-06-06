"""Tests for aegis_agent._reporter.MetricsReporter."""

from __future__ import annotations

import httpx
import respx

from aegis_agent._reporter import MetricsReporter

_METRICS = [
    {"name": "cpu_percent", "value": 42.0, "unit": "%", "tags": {}},
    {"name": "ram_percent", "value": 65.0, "unit": "%", "tags": {}},
]


class TestMetricsReporter:
    def _reporter(self, token: str = "") -> MetricsReporter:
        return MetricsReporter(
            backend_url="http://aegis-backend:8080",
            agent_token=token,
            hostname="test-host",
        )

    @respx.mock
    def test_report_posts_to_ingest_url(self) -> None:
        route = respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(202, json={"accepted": 2, "hostname": "test-host"})
        )
        result = self._reporter().report(_METRICS)
        assert result is True
        assert route.called

    @respx.mock
    def test_report_sends_bearer_token(self) -> None:
        route = respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(202, json={"accepted": 2, "hostname": "test-host"})
        )
        self._reporter(token="my-token").report(_METRICS)
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer my-token"

    @respx.mock
    def test_report_no_auth_header_when_no_token(self) -> None:
        route = respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(202, json={"accepted": 2, "hostname": "test-host"})
        )
        self._reporter(token="").report(_METRICS)
        req = route.calls.last.request
        assert "authorization" not in req.headers

    @respx.mock
    def test_report_returns_false_on_non_202(self) -> None:
        respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(401, json={"detail": "Unauthorized"})
        )
        result = self._reporter(token="bad").report(_METRICS)
        assert result is False

    @respx.mock
    def test_report_returns_true_on_empty_metrics(self) -> None:
        result = self._reporter().report([])
        assert result is True

    @respx.mock
    def test_report_returns_false_on_connection_error(self) -> None:
        respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = self._reporter().report(_METRICS)
        assert result is False

    @respx.mock
    def test_report_payload_contains_hostname(self) -> None:
        route = respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(202, json={"accepted": 2, "hostname": "test-host"})
        )
        self._reporter().report(_METRICS)
        import json

        body = json.loads(route.calls.last.request.content)
        assert body["hostname"] == "test-host"
        assert len(body["metrics"]) == 2

    @respx.mock
    def test_report_payload_includes_collected_at(self) -> None:
        route = respx.post("http://aegis-backend:8080/api/v1/metrics/ingest").mock(
            return_value=httpx.Response(202, json={"accepted": 1, "hostname": "test-host"})
        )
        self._reporter().report(_METRICS[:1])
        import json

        body = json.loads(route.calls.last.request.content)
        assert "collected_at" in body
        assert body["collected_at"].endswith("+00:00") or body["collected_at"].endswith("Z")
