"""Plugin registry — discovers AutoHeal plugins via importlib.metadata entry_points.

Plugins register under the "aegis.plugins" entry_points group by including:
    [project.entry-points."aegis.plugins"]
    "my-plugin" = "my_pkg.plugins:MyPlugin"
in their pyproject.toml. Any installed package with that group is auto-discovered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger(__name__)

_ENTRY_POINTS_GROUP = "aegis.plugins"


def get_plugin_callable(plugin_id: str) -> Callable[..., Any] | None:
    """Return the plugin class for *plugin_id*, or None if not found/loadable.

    Used as the plugin_registry callable in ActionPlannerEngine.
    """
    eps = entry_points(group=_ENTRY_POINTS_GROUP)
    for ep in eps:
        if ep.name == plugin_id:
            try:
                return ep.load()  # type: ignore[return-value]
            except Exception as exc:
                log.warning("plugin_load_failed plugin_id=%s err=%s", plugin_id, exc)
                return None
    log.debug("plugin_not_found plugin_id=%s", plugin_id)
    return None


def list_plugin_ids() -> list[str]:
    """Return all registered plugin IDs from installed packages."""
    return [ep.name for ep in entry_points(group=_ENTRY_POINTS_GROUP)]
