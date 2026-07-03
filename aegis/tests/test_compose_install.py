"""Compose-based app install: _compose_install materializes compose + .env and
brings the stack up via oprim.docker_compose_up (images pulled on demand)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from aegis.server.api.routers.apps import (
    InstallAppRequest,
    _compose_install,
    _pick_free_host_port,
)

COMPOSE = """services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
  app:
    image: nginx:alpine
    ports: ["${HOST_PORT}:80"]
"""


def test_compose_install_materializes_and_generates_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_up(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    import oprim

    monkeypatch.setattr(oprim, "docker_compose_up", fake_up)

    body = InstallAppRequest(
        app_name="demo",
        compose=COMPOSE,
        compose_env={
            "DB_PASSWORD": "__RANDOM__",
            "APP_KEY": "__APP_KEY_B64__",
            "TOKEN": "__HEX32__",
            "HOST_PORT": "18090",
        },
    )
    _compose_install(body, tmp_path, "unix:///var/run/docker.sock")

    appdir = tmp_path / "apps" / "demo"
    assert (appdir / "docker-compose.yml").read_text() == COMPOSE

    env = dict(
        line.split("=", 1) for line in (appdir / ".env").read_text().splitlines() if "=" in line
    )
    assert env["HOST_PORT"] == "18090"  # literal values pass through
    assert env["DB_PASSWORD"] not in ("", "__RANDOM__") and len(env["DB_PASSWORD"]) >= 20
    assert env["APP_KEY"].startswith("base64:")
    assert re.fullmatch(r"[0-9a-f]{32}", env["TOKEN"])  # __HEX32__

    # compose_up called once, pull-on-demand, project scoped to the app name.
    assert len(calls) == 1
    assert calls[0]["pull"] == "missing"
    assert calls[0]["project_name"] == "demo"


def test_pick_free_host_port_skips_used(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    class R:
        stdout = "0.0.0.0:18090->80/tcp\n:::18091->80/tcp, 0.0.0.0:18091->80/tcp\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    # 18090 and 18091 are taken -> next free is 18092.
    assert _pick_free_host_port(18090, "unix:///var/run/docker.sock") == 18092
    # a free preferred port is returned as-is.
    assert _pick_free_host_port(18099, "unix:///var/run/docker.sock") == 18099


def test_pick_free_host_port_falls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def boom(*a: object, **k: object) -> None:
        raise OSError("docker missing")

    monkeypatch.setattr(subprocess, "run", boom)
    assert _pick_free_host_port(18090, "unix:///var/run/docker.sock") == 18090


def test_compose_install_env_stable_across_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oprim

    monkeypatch.setattr(oprim, "docker_compose_up", lambda **k: {})
    body = InstallAppRequest(
        app_name="demo", compose=COMPOSE, compose_env={"DB_PASSWORD": "__RANDOM__"}
    )
    _compose_install(body, tmp_path, "unix:///var/run/docker.sock")
    first = (tmp_path / "apps" / "demo" / ".env").read_text()
    _compose_install(body, tmp_path, "unix:///var/run/docker.sock")
    # .env is not regenerated → the generated password stays stable.
    assert (tmp_path / "apps" / "demo" / ".env").read_text() == first
