"""Tests for capacity forecaster."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.orchestration.capacity import check_capacity_metrics

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")

_GROWING_SAMPLES = [10.0, 15.0, 22.0, 31.0, 42.0, 55.0, 70.0, 87.0]  # rapid growth
_STABLE_SAMPLES = [45.0, 46.0, 44.0, 45.0, 46.0, 45.0, 44.0, 46.0]  # stable


def _metric_rows(metric: str, samples: list[float]) -> list[dict]:
    return [
        {
            "metric_name": metric,
            "value": v,
            "unit": "%",
        }
        for v in samples
    ]


class TestCheckCapacityMetrics:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_metrics(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = []
        result = await check_capacity_metrics(conn=conn)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_stable_metrics(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = _metric_rows("disk_usage_percent", _STABLE_SAMPLES)

        with mock.patch(
            "aegis.server.orchestration.capacity.compute_capacity_forecast"
        ) as mock_forecast:
            from oskill import CapacityForecastResult, ForecastPoint

            mock_forecast.return_value = CapacityForecastResult(
                metric_name="disk_usage_percent",
                current_value=46.0,
                predicted_values=[
                    ForecastPoint(t_offset=i, predicted_value=46.0) for i in range(5)
                ],
                trend_slope=0.1,
                will_breach_threshold=False,
                breach_at_offset=None,
                recommendation="No action needed",
                narrative=None,
            )
            result = await check_capacity_metrics(conn=conn)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_warnings_for_growing_metrics(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = _metric_rows("disk_usage_percent", _GROWING_SAMPLES)

        with mock.patch(
            "aegis.server.orchestration.capacity.compute_capacity_forecast"
        ) as mock_forecast:
            from oskill import CapacityForecastResult, ForecastPoint

            mock_forecast.return_value = CapacityForecastResult(
                metric_name="disk_usage_percent",
                current_value=87.0,
                predicted_values=[
                    ForecastPoint(t_offset=i, predicted_value=87.0 + i * 5) for i in range(5)
                ],
                trend_slope=5.0,
                will_breach_threshold=True,
                breach_at_offset=6,
                recommendation="Add disk capacity",
                narrative="Disk usage is growing rapidly",
            )
            result = await check_capacity_metrics(conn=conn)

        assert len(result) == 1
        assert result[0].metric_name == "disk_usage_percent"
        assert result[0].will_breach_threshold is True

    @pytest.mark.asyncio
    async def test_skips_metrics_with_too_few_samples(self) -> None:
        conn = mock.AsyncMock()
        # Only 2 samples — not enough for a meaningful forecast
        conn.fetch.return_value = _metric_rows("ram_usage_percent", [80.0, 82.0])

        with mock.patch(
            "aegis.server.orchestration.capacity.compute_capacity_forecast"
        ) as mock_forecast:
            result = await check_capacity_metrics(conn=conn)

        mock_forecast.assert_not_called()
        assert result == []

    @pytest.mark.asyncio
    async def test_calls_alerter_on_breach(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = _metric_rows("disk_usage_percent", _GROWING_SAMPLES)
        mock_alerter = mock.MagicMock()

        with mock.patch(
            "aegis.server.orchestration.capacity.compute_capacity_forecast"
        ) as mock_forecast:
            from oskill import CapacityForecastResult, ForecastPoint

            mock_forecast.return_value = CapacityForecastResult(
                metric_name="disk_usage_percent",
                current_value=87.0,
                predicted_values=[
                    ForecastPoint(t_offset=i, predicted_value=87.0 + i * 5) for i in range(5)
                ],
                trend_slope=5.0,
                will_breach_threshold=True,
                breach_at_offset=6,
                recommendation="Add disk capacity",
                narrative="Disk usage is growing rapidly",
            )
            await check_capacity_metrics(conn=conn, alerter=mock_alerter)

        mock_alerter.fire.assert_called_once()
        breach_call = mock_alerter.fire.call_args
        assert "disk_usage_percent" in str(breach_call)

    @pytest.mark.asyncio
    async def test_no_alerter_still_runs_forecast(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = _metric_rows("disk_usage_percent", _GROWING_SAMPLES)

        with mock.patch(
            "aegis.server.orchestration.capacity.compute_capacity_forecast"
        ) as mock_forecast:
            from oskill import CapacityForecastResult

            mock_forecast.return_value = CapacityForecastResult(
                metric_name="disk_usage_percent",
                current_value=87.0,
                predicted_values=[],
                trend_slope=5.0,
                will_breach_threshold=True,
                breach_at_offset=6,
                recommendation="Add disk",
                narrative=None,
            )
            # No alerter passed — should not raise
            result = await check_capacity_metrics(conn=conn)

        assert len(result) == 1
