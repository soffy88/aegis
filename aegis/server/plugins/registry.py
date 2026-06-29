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


def list_plugins(*, include_stubs: bool = False) -> list[dict[str, Any]]:
    """Return runbook-compatible metadata dicts for installed plugins.

    Not-yet-implemented stub plugins (is_stub=True) are excluded by default so the
    catalog only advertises plugins that actually do something. Pass include_stubs=True
    to surface them (e.g. for an admin/debug view).
    """
    result = []
    for ep in entry_points(group=_ENTRY_POINTS_GROUP):
        try:
            cls = ep.load()
        except Exception as exc:
            log.warning("plugin_load_failed plugin_id=%s err=%s", ep.name, exc)
            continue
        is_stub = bool(getattr(cls, "is_stub", False))
        if is_stub and not include_stubs:
            continue
        result.append(
            {
                "name": getattr(cls, "name", ep.name),
                "description": getattr(cls, "description", ""),
                "trigger": getattr(cls, "matches_alert", ""),
                "requires_approval": getattr(cls, "requires_approval_when", None) is not None,
                "version": getattr(cls, "version", ""),
                "is_stub": is_stub,
                "steps": [],
                "source": "plugin",
            }
        )
    return result
