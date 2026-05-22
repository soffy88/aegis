"""Structured logging setup for Aegis main service.

Critical: BackgroundTasks run in the same event loop and use the same Python
logging configuration as the request handlers. We must ensure:
  1. Root logger has a stream handler so BackgroundTask exceptions show up.
  2. Uvicorn's loggers don't suppress our application loggers.
  3. The aegis.* hierarchy propagates to root.
"""
from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger.

    All Python `logging` calls (including those from `BackgroundTasks`
    via `log.exception(...)`) flow through here.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Clear default handlers (uvicorn may have added some)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level.upper())
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root.addHandler(handler)

    # Make sure the aegis.* hierarchy propagates
    for name in ("aegis", "omodul", "oskill", "oprim"):
        lg = logging.getLogger(name)
        lg.setLevel(level.upper())
        lg.propagate = True

    # Don't let uvicorn.access spam us at INFO; keep WARN+
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
