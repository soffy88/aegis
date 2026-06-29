"""Tests for plugins/registry.py — list_plugins() shape validation."""

from __future__ import annotations

from unittest import mock

from aegis.server.plugins.registry import list_plugins


def _make_plugin_class(
    *,
    name: str,
    description: str,
    matches_alert: str,
    version: str = "1.0.0",
    requires_approval_when: str | None = None,
) -> type:
    return type(
        "FakePlugin",
        (),
        {
            "name": name,
            "description": description,
            "matches_alert": matches_alert,
            "version": version,
            "requires_approval_when": requires_approval_when,
        },
    )


def _make_ep(name: str, cls: type) -> mock.MagicMock:
    ep = mock.MagicMock()
    ep.name = name
    ep.load.return_value = cls
    return ep


class TestListPlugins:
    def test_returns_list(self) -> None:
        with mock.patch(
            "aegis.server.plugins.registry.entry_points",
            return_value=[],
        ):
            result = list_plugins()
        assert isinstance(result, list)

    def test_entry_includes_name(self) -> None:
        cls = _make_plugin_class(name="test-plugin", description="", matches_alert="alert_type")
        ep = _make_ep("test-plugin", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["name"] == "test-plugin"

    def test_entry_includes_description(self) -> None:
        cls = _make_plugin_class(name="p", description="A description", matches_alert="x")
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["description"] == "A description"

    def test_entry_includes_trigger_from_matches_alert(self) -> None:
        cls = _make_plugin_class(name="p", description="", matches_alert="container_unhealthy")
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["trigger"] == "container_unhealthy"

    def test_requires_approval_false_when_no_condition(self) -> None:
        cls = _make_plugin_class(
            name="p", description="", matches_alert="x", requires_approval_when=None
        )
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["requires_approval"] is False

    def test_requires_approval_true_when_condition_set(self) -> None:
        cls = _make_plugin_class(
            name="p",
            description="",
            matches_alert="x",
            requires_approval_when="destructive",
        )
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["requires_approval"] is True

    def test_entry_includes_version(self) -> None:
        cls = _make_plugin_class(name="p", description="", matches_alert="x", version="2.3.0")
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["version"] == "2.3.0"

    def test_entry_has_steps_empty_list(self) -> None:
        cls = _make_plugin_class(name="p", description="", matches_alert="x")
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["steps"] == []

    def test_entry_has_source_plugin(self) -> None:
        cls = _make_plugin_class(name="p", description="", matches_alert="x")
        ep = _make_ep("p", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["source"] == "plugin"

    def test_load_failure_skips_entry(self) -> None:
        ep = mock.MagicMock()
        ep.name = "bad-plugin"
        ep.load.side_effect = ImportError("not found")
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result == []


class TestStubFiltering:
    @staticmethod
    def _stub_class() -> type:
        return type(
            "FakeStub",
            (),
            {"name": "stubby", "description": "", "matches_alert": "x", "is_stub": True},
        )

    def test_stub_hidden_by_default(self) -> None:
        ep = _make_ep("stubby", self._stub_class())
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result == []

    def test_stub_included_when_requested(self) -> None:
        ep = _make_ep("stubby", self._stub_class())
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins(include_stubs=True)
        assert len(result) == 1
        assert result[0]["is_stub"] is True

    def test_real_plugin_not_marked_stub(self) -> None:
        cls = _make_plugin_class(name="real", description="", matches_alert="x")
        ep = _make_ep("real", cls)
        with mock.patch("aegis.server.plugins.registry.entry_points", return_value=[ep]):
            result = list_plugins()
        assert result[0]["is_stub"] is False
