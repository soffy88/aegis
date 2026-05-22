"""Tests for plugin_loader."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from aegis.server.exceptions import PluginLoadError
from aegis.server.runtime.plugin_loader import load_all_plugins


def _fake_ep(name: str, plugin_name: str, validates: bool = True) -> Any:
    ep = mock.MagicMock()
    ep.name = name
    plugin_cls = mock.MagicMock()
    plugin_cls.name = plugin_name
    plugin_cls.__name__ = f"Plugin_{plugin_name}"
    if validates:
        plugin_cls.validate_config = mock.MagicMock()
    else:
        plugin_cls.validate_config = mock.MagicMock(side_effect=ValueError("bad"))
    ep.load.return_value = plugin_cls
    return ep


class TestLoadAllPlugins:
    def test_loads_valid_plugins(self) -> None:
        with mock.patch(
            "aegis.server.runtime.plugin_loader.entry_points",
            return_value=[_fake_ep("ep1", "plugin-a"), _fake_ep("ep2", "plugin-b")],
        ):
            result = load_all_plugins()
        assert len(result) == 2
        assert "plugin-a" in result

    def test_skips_validation_failures(self) -> None:
        with mock.patch(
            "aegis.server.runtime.plugin_loader.entry_points",
            return_value=[_fake_ep("ep1", "good"), _fake_ep("ep2", "bad", validates=False)],
        ):
            result = load_all_plugins()
        assert "good" in result
        assert "bad" not in result

    def test_skips_load_failures(self) -> None:
        bad_ep = mock.MagicMock()
        bad_ep.name = "ep1"
        bad_ep.load.side_effect = ImportError("missing")

        with mock.patch(
            "aegis.server.runtime.plugin_loader.entry_points",
            return_value=[bad_ep, _fake_ep("ep2", "good")],
        ):
            result = load_all_plugins()
        assert "good" in result
        assert len(result) == 1

    def test_duplicate_name_raises(self) -> None:
        with (
            mock.patch(
                "aegis.server.runtime.plugin_loader.entry_points",
                return_value=[_fake_ep("ep1", "dup"), _fake_ep("ep2", "dup")],
            ),
            pytest.raises(PluginLoadError, match="Duplicate"),
        ):
            load_all_plugins()
