"""Filesystem layout and external-tool discovery for Decache (Linux port)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass
class Paths:
    """Resolved locations used throughout a run."""

    root: str            # the Decache program directory (where decache.py lives)
    bin_dir: str
    data_dir: str        # bin/data — the lost-media database
    history_temp: str    # bin/history/temp — scratch for history visit grouping
    verified: str        # ./Verified
    unverified: str      # ./Unverified
    work_dir: str        # bin — scratch for frames.raw etc.

    @classmethod
    def discover(cls, root: str) -> "Paths":
        bin_dir = os.path.join(root, "bin")
        p = cls(
            root=root,
            bin_dir=bin_dir,
            data_dir=os.path.join(bin_dir, "data"),
            history_temp=os.path.join(bin_dir, "history", "temp"),
            verified=os.path.join(root, "Verified"),
            unverified=os.path.join(root, "Unverified"),
            work_dir=bin_dir,
        )
        return p

    def ensure_dirs(self) -> None:
        for d in (self.verified, self.unverified, self.history_temp):
            os.makedirs(d, exist_ok=True)


@dataclass
class Tools:
    ffmpeg: str
    phash: str
    archiver: str        # "7z", "7za", or "zip"

    @classmethod
    def discover(cls, bin_dir: str) -> "Tools":
        # ffmpeg: prefer system, this is native on Linux.
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        # phash: the helper we compile via build.sh, kept in bin/.
        local_phash = os.path.join(bin_dir, "phash")
        phash = local_phash if os.path.exists(local_phash) else (shutil.which("phash") or local_phash)
        # archiver: 7z/7za preferred (zip output), fall back to zip.
        archiver = shutil.which("7z") or shutil.which("7za") or shutil.which("zip") or "zip"
        return cls(ffmpeg=ffmpeg, phash=os.path.basename(phash) and phash, archiver=archiver)
