"""Discover AutoHealPlugin entry_points + validate config."""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

from aegis.server.exceptions import PluginLoadError

log = logging.getLogger(__name__)


def load_all_plugins() -> dict[str, type[Any]]:
    """Discover all AutoHealPlugin classes registered via entry_points.

    Returns:
        dict {plugin.name -> plugin class}. Plugins that fail validate_config()
        are logged and skipped.
    """
    plugins: dict[str, type[Any]] = {}
    discovered = entry_points(group="aegis.plugins")
    for ep in discovered:
        try:
            cls = ep.load()
        except Exception as exc:  # noqa: BLE001
            log.error("plugin_load_failed entry=%s: %s", ep.name, exc)
            continue

        try:
            # AutoHealPlugin.validate_config is a classmethod from aegis-autoheal-sdk
            cls.validate_config()
        except Exception as exc:  # noqa: BLE001
            log.error("plugin_validation_failed plugin=%s: %s", ep.name, exc)
            continue

        name = getattr(cls, "name", None)
        if not name:
            log.error("plugin_missing_name entry=%s", ep.name)
            continue

        if name in plugins:
            raise PluginLoadError(f"Duplicate plugin name: {name!r}")
        plugins[name] = cls
        log.info("plugin_loaded name=%s class=%s", name, cls.__name__)

    return plugins
