"""Index-driven recovery — the faithful port of read_cache.bat.

The Windows version is fundamentally *index driven*: it reads each browser
cache's index (URL list), and for any entry whose URL matches the asset database
it copies the cached file straight into ``Verified`` (trusted, no frame/duration
verification needed), recording the URL in ``Verified/contents.txt`` and the
on-disk origin in ``bin/private_locations.txt``.  This is how it recovers Flash
games (``.swf``/``.dcr``/``.mov``) and known videos — not just magic-byte
videos.

Pipeline per cache location (mirrors read_cache.bat :main / :handleLine /
:unusedCheck):

A. **Watch-page mapping** — for each cached YouTube watch page whose id is in
   ``watch_page_data.txt``, pull the ``o-<44>`` asset id out of the page HTML and
   remember ``o-id -> original video id`` (the ``id_pairs`` map), adding the
   ``o-id`` to the asset search terms.
B. **Indexed-asset recovery** — any entry whose URL matches an asset term (a
   domain from ``asset_data.txt`` or a known ``o-id``) is copied directly into
   ``Verified`` as ``[N]filename`` with its (ip-redacted, ``video_id``-annotated)
   URL appended to ``contents.txt``.
C. **Unique-name assets** — remaining entries whose filename matches a
   ``unique_names.txt`` glob (e.g. ``*.swf``) are also copied to ``Verified``.
D. **Unindexed videos** — every remaining video entry is reassembled and
   verified by duration / perceptual hash / history (see :mod:`lib.reassemble`).

In all branches, YouTube ids seen in watch/videoplayback/get_video URLs are
MD5-hashed into the ``cached_ids`` set for the optional encrypted-id sharing
feature.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
from typing import Dict, List, Optional, Set

from . import packaging
from .cache_common import CacheEntry
from .database import Database
from .reassemble import Reassembler, suggested_name
from .verify import Verifier

log = logging.getLogger("decache.recover")

# Pull any video id (11-char YouTube or 16-hex) out of a cache URL.
URL_ID_RE = re.compile(r"[?&](?:video_id|id|v)=([\w-]{11}(?![\w-])|[0-9a-f]{16}(?![0-9a-f]))")
# Watch-page url -> 11-char id.
WATCH_V_RE = re.compile(r"[?&]v=([\w-]{11})")
# An o- asset id inside a (videoplayback) URL.
O_ID_IN_URL_RE = re.compile(r"[?&]id=(o-[\w-]{44})")
# The o- asset id inside watch-page HTML (& is JSON-escaped as &).
WATCH_OID_RE = re.compile(rb"u0026id=(o-[\w-]{44})")
# IP address parameter to redact from recovered URLs.
IP_PARAM_RE = re.compile(r"([?&]ip=)([0-9.:]+)")


def md5_id(video_id: str) -> str:
    """MD5 of a video id (matches md5.vbs)."""
    return hashlib.md5(video_id.encode("utf-8")).hexdigest()


class Recoverer:
    """Stateful, run-wide recovery across all scanned cache locations."""

    def __init__(self, db: Database, verifier: Verifier, verified_dir: str,
                 unverified_dir: str, private_locations_path: str, keep_all: bool):
        self.db = db
        self.verifier = verifier
        self.verified = verified_dir
        self.unverified = unverified_dir
        self.contents_path = os.path.join(verified_dir, "contents.txt")
        self.private_locations_path = private_locations_path
        self.keep_all = keep_all

        self.cached_ids: Set[str] = set()
        self.id_pairs: Dict[str, str] = {}            # o-id -> original video id
        self.asset_terms: List[str] = list(db.asset_terms)
        self._asset_terms_set: Set[str] = set(self.asset_terms)
        self.file_count = self._init_file_count()
        self.reasm = Reassembler(verifier, verified_dir, unverified_dir, keep_all)

        # Diagnostics (so a 0/0 run can be explained).
        self.seen_video_ids: Set[str] = set()   # raw ids, not hashed
        self.totals = {"entries": 0, "video_candidates": 0, "assets": 0}
        self.dump_fh = None                      # optional URL dump file handle

    # -- helpers -----------------------------------------------------------
    def _init_file_count(self) -> int:
        """Continue numbering from any existing contents.txt (as the bat did)."""
        try:
            with open(self.contents_path, "r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def _collect_cached_ids(self, url: str) -> None:
        for m in URL_ID_RE.finditer(url):
            self.cached_ids.add(md5_id(m.group(1)))
            self.seen_video_ids.add(m.group(1))

    def _is_asset_url(self, url_lower: str) -> bool:
        return any(term in url_lower for term in self.asset_terms)

    @staticmethod
    def _redact(url: str) -> str:
        return IP_PARAM_RE.sub(lambda m: m.group(1) + "REDACTED", url)

    # -- phase A: watch-page -> o-id mapping -------------------------------
    def _map_watch_pages(self, entries: List[CacheEntry]) -> None:
        watch_ids = set(self.db.watch_page_ids)
        if not watch_ids:
            return
        for entry in entries:
            url = entry.url or ""
            if "watch?" not in url and "/watch" not in url:
                continue
            m = WATCH_V_RE.search(url)
            if not m or m.group(1) not in watch_ids:
                continue
            body = entry.read_bytes()
            if not body:
                continue
            for om in WATCH_OID_RE.finditer(body):
                oid = om.group(1).decode("ascii", "replace")
                self.id_pairs.setdefault(oid, m.group(1))
                if oid.lower() not in self._asset_terms_set:
                    self._asset_terms_set.add(oid.lower())
                    self.asset_terms.append(oid.lower())

    # -- phase B/C: direct recovery to Verified ----------------------------
    def _recover_asset(self, entry: CacheEntry, url: str) -> None:
        """Copy an indexed/unique asset straight into Verified."""
        body = entry.read_bytes()
        if not body:
            return
        fixed_url = self._redact(url)
        om = O_ID_IN_URL_RE.search(url)
        if om and om.group(1) in self.id_pairs:
            fixed_url = f"{fixed_url}&video_id={self.id_pairs[om.group(1)]}"

        self.file_count += 1
        n = self.file_count
        raw_name = entry.filename or suggested_name(entry)
        out_name = packaging.free_name(self.verified, packaging.sanitize_filename(f"[{n}]{raw_name}"))
        out_path = os.path.join(self.verified, out_name)
        try:
            with open(out_path, "wb") as fh:
                fh.write(body)
        except OSError as exc:
            log.debug("could not write asset %s: %s", out_path, exc)
            return
        ts = entry.best_timestamp()
        if ts:
            packaging.set_mtime(out_path, ts)
        self._append_line(self.contents_path, f'"{n} {fixed_url}"')
        origin = entry.source_path or f"{entry.backend}:{url}"
        self._append_line(self.private_locations_path, f'"{n} {origin}"')
        log.info("recovered asset [%d] %s", n, raw_name)

    @staticmethod
    def _append_line(path: str, line: str) -> None:
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            log.debug("append to %s failed: %s", path, exc)

    def _matches_unique_glob(self, entry: CacheEntry) -> bool:
        name = (entry.filename or "").lower()
        if not name and entry.url:
            name = entry.url.split("?")[0].rstrip("/").split("/")[-1].lower()
        if not name:
            return False
        return any(fnmatch.fnmatch(name, g.lower()) for g in self.db.unique_globs)

    # -- main per-location driver ------------------------------------------
    def process_location(self, entries: List[CacheEntry], label: str = "") -> None:
        # Stable, timestamp-ordered processing so chunk reassembly is correct.
        entries = sorted(entries, key=lambda e: (e.best_timestamp() or 0.0))

        # Phase A.
        self._map_watch_pages(entries)

        n_entries = n_video = n_asset = 0
        for entry in entries:
            n_entries += 1
            self.totals["entries"] += 1
            url = entry.url or ""
            self._collect_cached_ids(url)
            if self.dump_fh is not None:
                self.dump_fh.write(f"{entry.backend}\t{url}\n")

            url_lower = url.lower()
            # Phase B: indexed asset (URL matches asset DB / known o-id).
            if url and self._is_asset_url(url_lower):
                self._recover_asset(entry, url)
                n_asset += 1
                self.totals["assets"] += 1
                continue

            # Phase C: unique-name asset (filename glob, e.g. *.swf).
            if self._matches_unique_glob(entry):
                self._recover_asset(entry, url)
                n_asset += 1
                self.totals["assets"] += 1
                continue

            # Phase D: unindexed video candidate -> reassemble + verify.
            body = entry.read_bytes()
            if not body:
                continue
            from . import magic
            kind = magic.classify(body)
            is_video = (kind is not magic.Kind.OTHER
                        or bool(re.search(r"/(?:watch|videoplayback|get_video)\?", url, re.I)))
            if not is_video:
                continue
            n_video += 1
            self.totals["video_candidates"] += 1
            self.reasm.feed(body, url, suggested_name(entry), entry.best_timestamp())

        log.info("  %s: %d entries, %d video candidate(s), %d asset(s) recovered",
                 label or "location", n_entries, n_video, n_asset)

    def flush(self) -> None:
        self.reasm.flush()

    @property
    def stats(self) -> dict:
        return self.reasm.stats
