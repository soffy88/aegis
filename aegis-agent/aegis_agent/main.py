"""CLI entry point for aegis-agent."""

from __future__ import annotations

import asyncio
import logging
import os

from aegis_agent._collector import collect_metrics
from aegis_agent._loop import run_loop
from aegis_agent._reporter import MetricsReporter


def cli_main() -> None:
    """Start the aegis-agent metrics collection loop."""
    log_level = os.environ.get("AEGIS_AGENT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    backend_url = os.environ.get("AEGIS_BACKEND_URL", "http://localhost:8080")
    agent_token = os.environ.get("AEGIS_AGENT_TOKEN", "")
    docker_host = os.environ.get("AEGIS_DOCKER_HOST", "unix:///var/run/docker.sock")
    interval = int(os.environ.get("AEGIS_AGENT_INTERVAL", "60"))

    reporter = MetricsReporter(backend_url=backend_url, agent_token=agent_token)

    def _collect() -> list:
        return collect_metrics(docker_host=docker_host)

    asyncio.run(run_loop(collect_fn=_collect, report_fn=reporter.report, interval_seconds=interval))


if __name__ == "__main__":
    cli_main()
