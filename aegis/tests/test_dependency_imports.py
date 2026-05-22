"""Smoke tests verifying cross-repo dependencies are installed."""

from __future__ import annotations


def test_obase_importable() -> None:
    """obase must be installed (3O base layer)."""
    import obase  # noqa: F401


def test_oprim_importable() -> None:
    """oprim must be installed (3O atomic ops)."""
    import oprim  # noqa: F401


def test_oskill_importable() -> None:
    """oskill must be installed (3O skill workflows)."""
    import oskill  # noqa: F401


def test_omodul_importable() -> None:
    """omodul must be installed (3O business modules)."""
    import omodul  # noqa: F401


def test_omodul_install_app_callable() -> None:
    """The critical omodul.install_app entry must be importable."""
    from omodul.install_app import install_app

    assert callable(install_app)


def test_aegis_autoheal_sdk_importable() -> None:
    """aegis-autoheal-sdk must be installed (Plugin ABC)."""
    import aegis_autoheal_sdk  # noqa: F401
