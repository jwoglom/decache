"""Collision-free filenames, mtime preservation, and Verified-folder zipping.

The Windows original leaned on xcopy/ren plus a VBScript that reset each file's
modified date so recovered media kept its original timestamp.  On Linux we use
``os.utime`` and the stdlib ``zipfile`` module (no external archiver needed,
keeping the dependency surface to "bash + python").
"""

from __future__ import annotations

import logging
import os
import zipfile
from typing import Optional

log = logging.getLogger("decache.packaging")


def free_name(directory: str, name: str) -> str:
    """Return ``name`` (or ``name (n)``) that does not yet exist in *directory*.

    Mirrors :getFreeName in start_decache.bat.
    """
    base, ext = os.path.splitext(name)
    candidate = name
    n = 0
    while os.path.exists(os.path.join(directory, candidate)):
        n += 1
        candidate = f"{base} ({n}){ext}"
    return candidate


def sanitize_filename(name: str) -> str:
    """Strip characters that are illegal in filenames on common systems."""
    out = name
    for ch in '/\\:*?"<>|':
        out = out.replace(ch, "-")
    return out.strip() or "video"


def preserve_mtime(target: str, source: str) -> None:
    """Copy ``source``'s mtime onto ``target`` (the un-modify-date behaviour)."""
    try:
        st = os.stat(source)
        os.utime(target, (st.st_atime, st.st_mtime))
    except OSError as exc:
        log.debug("could not preserve mtime %s -> %s: %s", source, target, exc)


def set_mtime(target: str, timestamp: float) -> None:
    try:
        os.utime(target, (timestamp, timestamp))
    except OSError as exc:
        log.debug("could not set mtime on %s: %s", target, exc)


def zip_folder(folder: str, out_dir: str, base_name: str = "Assets.zip") -> Optional[str]:
    """Zip every file in ``folder`` into a collision-free archive in ``out_dir``.

    Returns the archive path, or None if there was nothing to archive.
    """
    if not os.path.isdir(folder):
        return None
    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    if not files:
        return None

    name = free_name(out_dir, base_name)
    archive_path = os.path.join(out_dir, name)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(os.path.join(folder, f), arcname=f)
    except OSError as exc:
        log.error("failed to create archive %s: %s", archive_path, exc)
        return None
    log.info("wrote %s (%d files)", archive_path, len(files))
    return archive_path
