"""Tests for runtime.logging."""
from __future__ import annotations

import logging

from aegis.server.runtime.logging import setup_logging


class TestSetupLogging:
    def test_sets_level_info(self) -> None:
        setup_logging("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_sets_level_debug(self) -> None:
        setup_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_clears_existing_handlers(self) -> None:
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        setup_logging("WARNING")
        # After setup_logging, exactly one StreamHandler remains
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
