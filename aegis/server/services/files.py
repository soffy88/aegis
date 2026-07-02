"""Host filesystem file-manager service (sandboxed to configured roots).

Every operation validates that the target path resolves *inside* one of the
configured ``file_manager_roots`` via :func:`oprim.check_path_allowed`, which
``resolve()``s symlinks and rejects ``..`` escapes. A path outside the
whitelist raises :class:`PathNotAllowed` (→ HTTP 403 at the router layer).

The aegis-backend server runs in a container, so each configured root must also
be bind-mounted into the container at the *same* absolute path for these
operations to actually reach host files.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from aegis.server.runtime.config import get_settings

# Text view/edit ceiling — larger files must be downloaded, not opened inline.
MAX_TEXT_BYTES = 2 * 1024 * 1024  # 2 MiB
# Upload ceiling — a coarse guard against filling the disk via the browser.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB


class PathNotAllowed(Exception):
    """Target path is outside the configured ``file_manager_roots`` whitelist."""


class FileManagerDisabled(Exception):
    """No ``file_manager_roots`` configured — the feature is turned off."""


def get_roots() -> list[str]:
    """Return the configured whitelist roots (may be empty when disabled)."""
    return [str(Path(r)) for r in get_settings().file_manager_roots]


def _roots() -> list[Path]:
    roots = [Path(r) for r in get_settings().file_manager_roots]
    if not roots:
        raise FileManagerDisabled("file manager not configured (AEGIS_FILE_MANAGER_ROOTS is empty)")
    return roots


def _is_within(path: Path, root: Path) -> bool:
    """True if *path* equals or is beneath *root*, after resolving symlinks/``..``."""
    rp, rr = path.resolve(), root.resolve()
    return rp == rr or rp.is_relative_to(rr)


def _safe(path: str) -> Path:
    """Resolve *path* and assert it lies within an allowed root.

    ``Path.resolve()`` collapses ``..`` and follows symlinks in existing
    parents, so both traversal and symlink-escape land outside every root and
    are rejected. Works for not-yet-existing targets (mkdir/write/upload/rename
    dst) because ``resolve()`` handles an absent tail lexically.
    """
    p = Path(path)
    if not p.is_absolute():
        raise PathNotAllowed(f"path must be absolute: {path}")
    if not any(_is_within(p, r) for r in _roots()):
        raise PathNotAllowed(f"path outside allowed roots: {path}")
    return p


def list_dir(path: str, *, show_hidden: bool = True) -> dict[str, Any]:
    """List a single directory level with per-entry metadata."""
    p = _safe(path)
    if not p.exists():
        raise FileNotFoundError(f"not found: {path}")
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")

    entries: list[dict[str, Any]] = []
    with os.scandir(p) as it:
        for entry in it:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            entries.append(
                {
                    "name": entry.name,
                    "path": str(p / entry.name),
                    "is_dir": is_dir,
                    "is_symlink": entry.is_symlink(),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mode": oct(st.st_mode & 0o777),
                }
            )
    # Directories first, then case-insensitive by name.
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    # No parent link when the current dir is itself a whitelist root (can't
    # navigate above the sandbox) or the filesystem root.
    is_root = any(p.resolve() == r.resolve() for r in _roots()) or p == p.parent
    return {
        "path": str(p),
        "parent": None if is_root else str(p.parent),
        "entries": entries,
    }


def read_text(path: str) -> dict[str, Any]:
    """Read a small text file for inline viewing/editing."""
    p = _safe(path)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    size = p.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ValueError(
            f"file too large to view inline ({size} bytes > {MAX_TEXT_BYTES}); download it instead"
        )
    return {
        "path": str(p),
        "size": size,
        "content": p.read_text(encoding="utf-8", errors="replace"),
    }


def write_text(path: str, content: str) -> dict[str, Any]:
    """Overwrite (or create within an existing dir) a text file."""
    p = _safe(path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("content too large")
    if p.is_dir():
        raise IsADirectoryError(f"is a directory: {path}")
    # Never fabricate a parent tree from a typo'd path — parent must exist.
    if not p.parent.is_dir():
        raise FileNotFoundError(f"parent directory does not exist: {p.parent}")
    p.write_bytes(encoded)
    return {"path": str(p), "size": len(encoded)}


def make_dir(path: str) -> dict[str, Any]:
    """Create a directory (parents allowed, all within the sandbox root)."""
    p = _safe(path)
    if p.exists():
        raise FileExistsError(f"already exists: {path}")
    p.mkdir(parents=True)
    return {"path": str(p)}


def upload_file(dest_dir: str, filename: str, data: bytes) -> dict[str, Any]:
    """Write uploaded *data* as *filename* into *dest_dir*."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload too large (> {MAX_UPLOAD_BYTES} bytes)")
    d = _safe(dest_dir)
    if not d.is_dir():
        raise NotADirectoryError(f"not a directory: {dest_dir}")
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise ValueError(f"invalid filename: {filename!r}")
    target = _safe(str(d / safe_name))
    target.write_bytes(data)
    return {"path": str(target), "size": len(data)}


def rename_path(src: str, dst: str) -> dict[str, Any]:
    """Rename/move *src* to *dst*; both must be inside the sandbox."""
    s = _safe(src)
    d = _safe(dst)
    if not s.exists():
        raise FileNotFoundError(f"not found: {src}")
    if d.exists():
        raise FileExistsError(f"target already exists: {dst}")
    shutil.move(str(s), str(d))
    return {"src": str(s), "dst": str(d)}


def delete_path(path: str) -> dict[str, Any]:
    """Delete a file (or recursively a directory) inside the sandbox."""
    p = _safe(path)
    if not p.exists():
        raise FileNotFoundError(f"not found: {path}")
    if any(p.resolve() == r.resolve() for r in _roots()):
        raise PathNotAllowed(f"refusing to delete a configured root: {path}")
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()  # files and symlinks
    return {"path": str(p), "deleted": True}


def resolve_for_download(path: str) -> Path:
    """Validate *path* is a readable file inside the sandbox; return its Path."""
    p = _safe(path)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    return p
