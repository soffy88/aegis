"""Lightweight anomaly detection over a metric series (EWMA z-score).

Maintains an exponentially-weighted mean + variance and flags the latest point when
it deviates more than ``z_threshold`` standard deviations from the running baseline.
This catches drift/spikes a static threshold misses (e.g. CPU jumping from its usual
20% to 60% — still under a 90% threshold but clearly anomalous).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class AnomalyResult:
    is_anomaly: bool
    score: float  # z-score of the latest point vs the running baseline
    value: float
    baseline: float  # EWMA mean before the latest point
    std: float
    n: int


def ewma_anomaly(
    values: list[float],
    *,
    alpha: float = 0.3,
    z_threshold: float = 3.0,
    warmup: int = 5,
) -> AnomalyResult | None:
    """Evaluate whether the LAST value is anomalous vs an EWMA baseline.

    Returns None if there are fewer than ``warmup`` prior points to learn a baseline.
    """
    if len(values) < warmup + 1:
        return None

    ewma = values[0]
    ewvar = 0.0
    # Learn the baseline from all points except the last.
    for v in values[1:-1]:
        resid = v - ewma
        ewvar = alpha * (resid * resid) + (1 - alpha) * ewvar
        ewma = alpha * v + (1 - alpha) * ewma

    last = values[-1]
    std = math.sqrt(ewvar)
    resid = last - ewma
    score = resid / std if std > 1e-9 else 0.0
    return AnomalyResult(
        is_anomaly=abs(score) >= z_threshold,
        score=round(score, 3),
        value=last,
        baseline=round(ewma, 4),
        std=round(std, 4),
        n=len(values),
    )
