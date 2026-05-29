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
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

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


def _sniff(path: str):
    """Classify a file by its leading bytes (for picking an extension)."""
    try:
        with open(path, "rb") as fh:
            return magic.classify(fh.read(magic.HEADER_BYTES))
    except OSError:
        return magic.Kind.OTHER


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
        # Completed candidate files awaiting (parallel) verification.
        # Each: {"path", "meta", "owns"} — owns=True means we created the file
        # (move/delete freely); owns=False is a loose backup file (copy only).
        self.completed: List[dict] = []

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

    # -- completion (verification is deferred + parallelised) --------------
    def _finalize(self, path: Optional[str]) -> None:
        """Record a completed reassembled file; verification happens later."""
        if not path or not os.path.exists(path):
            if path:
                self._clear(path)
            return
        self.completed.append({"path": path, "meta": dict(self._meta.get(path, {})), "owns": True})
        self._clear(path)

    def register_loose(self, path: str, ts: Optional[float]) -> None:
        """Register a loose backup video file for verification (copied, never moved)."""
        self.completed.append({
            "path": path,
            "meta": {"suggested": os.path.basename(path), "ts": ts},
            "owns": False,
        })

    def verify_all(self, jobs: int = 1) -> None:
        """Verify every completed candidate (in parallel) and file the results."""
        items = self.completed
        self.completed = []
        if not items:
            return
        paths = [it["path"] for it in items]
        if jobs and jobs > 1 and len(paths) > 1:
            with ThreadPoolExecutor(max_workers=jobs) as ex:
                results = list(ex.map(self.verifier.verify, paths))
        else:
            results = [self.verifier.verify(p) for p in paths]
        # Classification (file moves/copies, naming) is done sequentially to
        # keep free_name() and the stats counters race-free.
        for it, result in zip(items, results):
            self._classify(it, result)

    def _classify(self, item: dict, result: MatchResult) -> None:
        path, meta, owns = item["path"], item["meta"], item["owns"]
        if not os.path.exists(path):
            return
        suggested = meta.get("suggested") or os.path.basename(path)
        if not owns:  # loose file: give it the detected container extension
            suggested = ensure_ext(suggested, magic.extension_for(_sniff(path)))
        origname = packaging.sanitize_filename(suggested)

        if result.status == "verified":
            self._place(item, self.verified, f"{result.title}{SEP_MARK}{origname}")
            self.stats["verified"] += 1
            log.info("VERIFIED: %s", result.title)
        elif result.status == "likely":
            self._place(item, self.unverified, f"{result.title}{LIKELY_MARK}{origname}")
            self.stats["likely"] += 1
            log.info("likely: %s", result.title)
        elif result.status == "unverified":
            self._place(item, self.unverified, f"{result.title}{SEP_MARK}{origname}")
            self.stats["unverified"] += 1
            log.info("unverified: %s", result.title)
        else:  # discard
            if self.keep_all:
                self._place(item, self.unverified, origname)
                self.stats["unverified"] += 1
            elif owns:
                try:
                    os.remove(path)
                except OSError:
                    pass
                self.stats["discarded"] += 1
            else:
                self.stats["discarded"] += 1  # leave the loose backup file alone

    def _place(self, item: dict, folder: str, base: str) -> None:
        """Move (owned) or copy (loose) a candidate into ``folder`` as ``base``."""
        src, ts, owns = item["path"], item["meta"].get("ts"), item["owns"]
        dst = os.path.join(folder, packaging.free_name(folder, base))
        try:
            if owns:
                os.replace(src, dst)
            else:
                shutil.copy2(src, dst)
        except OSError as exc:
            log.debug("place %s -> %s failed: %s", src, dst, exc)
            return
        if ts:
            packaging.set_mtime(dst, ts)

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
