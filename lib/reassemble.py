"""Chunk reassembly + verification for *unindexed* cache videos.

Browser caches store large media in ~1 MiB chunks; a chunk of exactly the cache
chunk size means more follow.  This reproduces start_decache.bat's
:checkFile / :scanDir accumulation: WebM/MP4 fragment headers and raw
continuation chunks are appended to the in-progress file of the right type,
then each completed file is run through the verifier and filed into
Verified / Unverified (or discarded).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional

from . import magic, packaging
from .cache_common import CacheEntry
from .verify import MatchResult, Verifier

log = logging.getLogger("decache.reassemble")

# Naming markers used by start_decache.bat (and the likely->verified promotion).
LIKELY_MARK = " @1@ "
SEP_MARK = " @ "

VIDEO_URL_RE = re.compile(r"/(?:watch|videoplayback|get_video)\?", re.IGNORECASE)
VIDEO_EXTS = (".webm", ".mp4", ".flv", ".m4v", ".mov", ".mkv", ".on2")


def ensure_ext(name: str, ext: Optional[str]) -> str:
    """Give ``name`` the detected container extension if it lacks a video one."""
    if not ext:
        return name
    if name.lower().endswith(VIDEO_EXTS):
        return name
    base = os.path.splitext(name)[0] if "." in os.path.basename(name) else name
    return base + ext


def suggested_name(entry: CacheEntry) -> str:
    if entry.filename:
        return entry.filename
    if entry.url:
        tail = entry.url.split("?")[0].rstrip("/").split("/")[-1]
        if tail:
            return tail
    return "video"


class Reassembler:
    def __init__(self, verifier: Verifier, verified_dir: str, unverified_dir: str,
                 keep_all: bool):
        self.verifier = verifier
        self.verified = verified_dir
        self.unverified = unverified_dir
        self.keep_all = keep_all
        self.current: Optional[str] = None
        self.pending_webm: Optional[str] = None
        self.pending_mp4: Optional[str] = None
        self._meta: Dict[str, dict] = {}
        self.stats = {"verified": 0, "likely": 0, "unverified": 0, "discarded": 0}

    # -- low-level file ops ------------------------------------------------
    def _save(self, body: bytes, suggested: str, ts: Optional[float],
              ext: Optional[str] = None) -> str:
        suggested = ensure_ext(suggested, ext)
        name = packaging.free_name(self.unverified, packaging.sanitize_filename(suggested))
        path = os.path.join(self.unverified, name)
        with open(path, "wb") as fh:
            fh.write(body)
        if ts:
            packaging.set_mtime(path, ts)
        self._meta[path] = {"suggested": suggested, "ts": ts}
        return path

    def _append(self, path: str, body: bytes) -> None:
        if not path or not os.path.exists(path):
            return
        meta = self._meta.get(path, {})
        with open(path, "ab") as fh:
            fh.write(body)
        if meta.get("ts"):
            packaging.set_mtime(path, meta["ts"])

    def _clear(self, path: str) -> None:
        if self.current == path:
            self.current = None
        if self.pending_webm == path:
            self.pending_webm = None
        if self.pending_mp4 == path:
            self.pending_mp4 = None
        self._meta.pop(path, None)

    # -- finalisation ------------------------------------------------------
    def _finalize(self, path: Optional[str]) -> None:
        if not path or not os.path.exists(path):
            if path:
                self._clear(path)
            return
        meta = self._meta.get(path, {})
        result = self.verifier.verify(path)
        self._classify(path, meta, result)
        self._clear(path)

    def _classify(self, path: str, meta: dict, result: MatchResult) -> None:
        origname = packaging.sanitize_filename(meta.get("suggested") or os.path.basename(path))
        if result.status == "verified":
            newname = packaging.free_name(self.verified, f"{result.title}{SEP_MARK}{origname}")
            self._move(path, os.path.join(self.verified, newname))
            self.stats["verified"] += 1
            log.info("VERIFIED: %s", result.title)
        elif result.status == "likely":
            newname = packaging.free_name(self.unverified, f"{result.title}{LIKELY_MARK}{origname}")
            self._move(path, os.path.join(self.unverified, newname))
            self.stats["likely"] += 1
            log.info("likely: %s", result.title)
        elif result.status == "unverified":
            newname = packaging.free_name(self.unverified, f"{result.title}{SEP_MARK}{origname}")
            self._move(path, os.path.join(self.unverified, newname))
            self.stats["unverified"] += 1
            log.info("unverified: %s", result.title)
        else:  # discard
            if self.keep_all:
                self.stats["unverified"] += 1
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
                self.stats["discarded"] += 1

    def _move(self, src: str, dst: str) -> None:
        meta = self._meta.get(src, {})
        try:
            os.replace(src, dst)
        except OSError as exc:
            log.debug("move %s -> %s failed: %s", src, dst, exc)
            return
        if meta.get("ts"):
            packaging.set_mtime(dst, meta["ts"])

    # -- the feed ----------------------------------------------------------
    def feed(self, body: bytes, url: str, suggested: str, ts: Optional[float]) -> None:
        if not body:
            return
        kind = magic.classify(body)
        size = len(body)
        is_video_url = bool(VIDEO_URL_RE.search(url or ""))

        if kind is magic.Kind.WEBM:
            self._finalize(self.pending_webm)
            f = self._save(body, suggested or "video", ts, ".webm")
            self.pending_webm = f
            self.current = f
        elif kind is magic.Kind.MP4:
            self._finalize(self.pending_mp4)
            f = self._save(body, suggested or "video", ts, ".mp4")
            self.pending_mp4 = f
            self.current = f
        elif kind is magic.Kind.FLV:
            self._finalize(self.current)
            f = self._save(body, suggested or "video", ts, ".flv")
            self.current = f
        elif kind is magic.Kind.WEBM_FRAG:
            if self.pending_webm:
                self._append(self.pending_webm, body)
                self.current = self.pending_webm
            else:
                return
        elif kind is magic.Kind.MP4_FRAG:
            if self.pending_mp4:
                self._append(self.pending_mp4, body)
                self.current = self.pending_mp4
            else:
                return
        else:  # OTHER
            if self.current:
                self._append(self.current, body)
            elif is_video_url:
                self.current = self._save(body, suggested or "video.bin", ts)
            else:
                return

        # A chunk shorter than the cache chunk size is the tail -> finalise.
        if self.current and not magic.is_full_chunk(size):
            self._finalize(self.current)

    def flush(self) -> None:
        for path in (self.current, self.pending_webm, self.pending_mp4):
            self._finalize(path)
