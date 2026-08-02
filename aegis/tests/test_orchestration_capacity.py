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
    async def test_thresholds_and_min_samples_come_from_config(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = _metric_rows(
            "disk_usage_percent", [10.0, 20.0, 30.0]
        )  # 3 samples

        cfg = mock.MagicMock()
        cfg.capacity_min_samples = 3  # lowered so 3 samples are enough
        cfg.capacity_default_threshold = 90.0
        cfg.capacity_breach_days_warn = 14
        cfg.capacity_metric_thresholds = {"disk_usage_percent": 75.0}

        with (
            mock.patch("aegis.server.orchestration.capacity.get_settings", return_value=cfg),
            mock.patch(
                "aegis.server.orchestration.capacity.compute_capacity_forecast"
            ) as mock_forecast,
        ):
            from oskill import CapacityForecastResult

            mock_forecast.return_value = CapacityForecastResult(
                metric_name="disk_usage_percent",
                current_value=30.0,
                predicted_values=[],
                trend_slope=1.0,
                will_breach_threshold=False,
                breach_at_offset=None,
                recommendation="ok",
                narrative=None,
            )
            await check_capacity_metrics(conn=conn)

        # config min_samples=3 → forecast ran; threshold + horizon came from config
        mock_forecast.assert_called_once()
        kwargs = mock_forecast.call_args.kwargs
        assert kwargs["threshold"] == 75.0
        assert kwargs["forecast_steps"] == 14

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


# ── memory bounds (prod OOM regression) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_capacity_aggregates_in_postgres_not_in_python() -> None:
    """Regression: this loop used to SELECT every raw row in the 48h window — 10.7M
    rows / ~1.1GB in production — and silently OOM-killed the container. The window
    must be bucketed server-side so memory stays flat regardless of ingest rate."""
    from aegis.server.orchestration import capacity as cap

    conn = mock.AsyncMock()
    conn.fetch.return_value = []
    await cap.check_capacity_metrics(conn=conn)

    sql = conn.fetch.await_args.args[0]
    assert "date_bin" in sql and "GROUP BY" in sql
    assert "avg(value)" in sql
    assert conn.fetch.await_args.args[1] == f"{cap._BUCKET_MINUTES} minutes"


@pytest.mark.asyncio
async def test_capacity_caps_buckets_and_metric_cardinality() -> None:
    """Even with a pathological result set, per-metric points and metric count are capped."""
    from aegis.server.orchestration import capacity as cap

    rows = [{"metric_name": "cpu", "value": float(i), "unit": ""} for i in range(5_000)]
    rows += [
        {"metric_name": f"m{i}", "value": 1.0, "unit": ""} for i in range(cap._MAX_METRICS + 50)
    ]
    conn = mock.AsyncMock()
    conn.fetch.return_value = rows

    captured: dict[str, list[float]] = {}

    def fake_forecast(*, metric_name, samples, threshold, forecast_steps):
        captured[metric_name] = samples
        return mock.MagicMock(will_breach_threshold=False)

    with mock.patch.object(cap, "compute_capacity_forecast", side_effect=fake_forecast):
        await cap.check_capacity_metrics(conn=conn)

    assert len(captured["cpu"]) <= cap._MAX_BUCKETS_PER_METRIC
    assert len(captured) <= cap._MAX_METRICS
