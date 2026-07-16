"""ADR-004 P2 — site scaffolding: template files land on disk; nextjs-oui vendors
the private OUI tarballs and wires package.json to them; error paths are loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.server.services.site_scaffold import scaffold


def test_static_writes_html(tmp_path: Path) -> None:
    dest = tmp_path / "s"
    files = scaffold("static", dest)
    assert "index.html" in files
    assert (dest / "index.html").read_text().startswith("<!doctype html>")


def test_php_writes_php(tmp_path: Path) -> None:
    dest = tmp_path / "p"
    files = scaffold("php", dest)
    assert files == ["index.php"]
    assert "<?=" in (dest / "index.php").read_text()


def _make_vendor(tmp_path: Path, *names: str) -> Path:
    vendor = tmp_path / "oui"
    vendor.mkdir()
    for n in names:
        (vendor / n).write_bytes(b"tgz")
    return vendor


def test_nextjs_vendors_tgz_and_refs_them(tmp_path: Path) -> None:
    vendor = _make_vendor(tmp_path, "helios-blocks-4.4.0.tgz", "helios-oui-2.1.5.tgz")
    dest = tmp_path / "site"
    scaffold("nextjs-oui", dest, oui_vendor_dir=str(vendor))

    assert (dest / "vendor" / "helios-blocks-4.4.0.tgz").exists()
    pkg = (dest / "package.json").read_text()
    assert "file:./vendor/helios-blocks-4.4.0.tgz" in pkg
    assert "file:./vendor/helios-oui-2.1.5.tgz" in pkg
    assert "output: 'export'" in (dest / "next.config.mjs").read_text()
    assert "OStatusBadge" in (dest / "app" / "page.tsx").read_text()


def test_nextjs_picks_latest_tgz(tmp_path: Path) -> None:
    vendor = _make_vendor(
        tmp_path, "helios-blocks-4.3.0.tgz", "helios-blocks-4.4.0.tgz", "helios-oui-2.1.5.tgz"
    )
    dest = tmp_path / "site"
    scaffold("nextjs-oui", dest, oui_vendor_dir=str(vendor))
    assert "helios-blocks-4.4.0.tgz" in (dest / "package.json").read_text()


def test_unknown_template_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown template"):
        scaffold("django", tmp_path / "x")


def test_non_empty_dest_raises(tmp_path: Path) -> None:
    dest = tmp_path / "x"
    dest.mkdir()
    (dest / "f").write_text("hi")
    with pytest.raises(ValueError, match="not empty"):
        scaffold("static", dest)


def test_nextjs_without_vendor_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="AEGIS_OUI_VENDOR_DIR"):
        scaffold("nextjs-oui", tmp_path / "s", oui_vendor_dir="")


def test_nextjs_missing_tgz_raises(tmp_path: Path) -> None:
    vendor = tmp_path / "oui"
    vendor.mkdir()  # empty — no tarballs
    with pytest.raises(ValueError, match="helios-blocks"):
        scaffold("nextjs-oui", tmp_path / "s", oui_vendor_dir=str(vendor))
