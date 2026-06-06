"""aegis-agent — metrics collection agent for the Aegis platform.

Collects host and container metrics via oprim and reports them to the
Aegis backend POST /api/v1/metrics/ingest every 60 seconds.
"""

from aegis_agent._collector import collect_metrics
from aegis_agent._loop import run_loop
from aegis_agent._reporter import MetricsReporter

__version__ = "0.1.0"

__all__ = ["collect_metrics", "run_loop", "MetricsReporter", "__version__"]
