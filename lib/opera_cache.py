"""Presto-era Opera (pre-Blink) disk-cache parser -- best effort only.

Old Opera (the Presto engine, roughly Opera 9-12) kept its disk cache in a
directory keyed by an index file ``dcache4.url`` alongside data files named like
``g_NNNN``, ``k_NNNN``, ``f_NNNN``, ``opr<hex>``, ``sesn``, or plain hashed
names.  Each data file holds one or more raw HTTP response bodies, occasionally
prefixed by a small Opera-internal header.

The on-disk format is poorly documented and, crucially, the mapping from a URL
(in ``dcache4.url``) to the file that holds its body is unreliable -- the
original Windows tool this is ported from gave up on Opera history entirely.
We therefore do **not** attempt a faithful URL->file association.

Strategy
--------
* :func:`detect` -- ``True`` when the directory contains ``dcache4.url``.
* :func:`parse` -- yield one :class:`CacheEntry` per regular data file in the
  cache dir (excluding ``dcache4.url`` / ``dcache4.lck`` and other index/lock
  files), with ``source_path`` pointing at the file, ``data=None``,
  ``last_modified`` set to the file mtime, and ``url=""``.  We additionally scan
  ``dcache4.url`` for embedded ``http`` strings and, when the counts line up,
  attach them in order; otherwise ``url`` is left empty.

Yielding the raw files is the point: the downstream pipeline detects videos by
file magic bytes regardless of URL, so an empty ``url`` is acceptable.

Pure standard library; never raises out of :func:`parse`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterator, List, Optional

from .cache_common import CacheEntry

log = logging.getLogger("decache.opera")

# Index / lock / bookkeeping files that are not cached bodies.
_INDEX_FILE = "dcache4.url"
_SKIP_NAMES = {
    "dcache4.url",
    "dcache4.lck",
    "dcache4.dat",
    "vlink4.dat",
    "cookies4.dat",
    "download.dat",
    "operaprefs.ini",
}

# Embedded URL scanner for dcache4.url (URLs are stored as plain ASCII runs).
_HTTP_URL_RE = re.compile(rb"https?://[^\x00-\x20\"'<>\\]+")


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def detect(path: str) -> bool:
    """Return ``True`` if *path* is a Presto Opera cache directory.

    Accepts the cache directory itself (containing ``dcache4.url``) or being
    handed the ``dcache4.url`` file directly.
    """
    try:
        if os.path.isfile(path) and os.path.basename(path) == _INDEX_FILE:
            return True
        if os.path.isdir(path):
            return os.path.isfile(os.path.join(path, _INDEX_FILE))
    except OSError as exc:
        log.debug("detect(%r) failed: %s", path, exc)
    return False


# --------------------------------------------------------------------------- #
# Public parse entry point
# --------------------------------------------------------------------------- #
def parse(path: str) -> Iterator[CacheEntry]:
    """Yield :class:`CacheEntry` objects for the Opera cache at *path*.

    Best-effort: every data file in the directory is surfaced via
    ``source_path``.  URL association is unreliable and usually left empty.
    Never raises.
    """
    try:
        cache_dir = _resolve_dir(path)
        if cache_dir is None:
            log.debug("parse(%r): not an Opera cache dir", path)
            return

        try:
            names = sorted(os.listdir(cache_dir))
        except OSError as exc:
            log.debug("cannot list %r: %s", cache_dir, exc)
            return

        # Collect candidate data files (regular files, not index/lock/bookkeeping).
        data_files: List[str] = []
        for name in names:
            full = os.path.join(cache_dir, name)
            try:
                if not os.path.isfile(full):
                    continue
            except OSError:
                continue
            if name.lower() in _SKIP_NAMES:
                continue
            data_files.append(full)

        # Best-effort URL recovery from the index (order only; may not align).
        urls = _scan_index_urls(os.path.join(cache_dir, _INDEX_FILE))
        # Only zip URLs onto files if the counts plausibly correspond; this is
        # admittedly a heuristic and frequently produces url="" (which is fine).
        associate = len(urls) == len(data_files) and len(urls) > 0
        if urls and not associate:
            log.debug(
                "opera: %d URLs vs %d data files in %r; leaving urls empty",
                len(urls),
                len(data_files),
                cache_dir,
            )

        for i, full in enumerate(data_files):
            try:
                url = urls[i] if associate else ""
                yield CacheEntry(
                    backend="opera",
                    url=url,
                    source_path=full,
                    data=None,
                    last_modified=_safe_mtime(full),
                    filename=os.path.basename(full),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort per file
                log.debug("opera data file %r failed: %s", full, exc)
                continue
    except Exception as exc:  # noqa: BLE001 - parse() must never raise
        log.debug("opera parse(%r) aborted: %s", path, exc)
        return


def _resolve_dir(path: str) -> Optional[str]:
    """Resolve *path* to the cache directory containing ``dcache4.url``."""
    if not path:
        return None
    if os.path.isfile(path) and os.path.basename(path) == _INDEX_FILE:
        return os.path.dirname(os.path.abspath(path))
    if os.path.isdir(path) and os.path.isfile(os.path.join(path, _INDEX_FILE)):
        return path
    return None


def _scan_index_urls(index_path: str) -> List[str]:
    """Scan ``dcache4.url`` for embedded ``http(s)`` URL strings, in order."""
    urls: List[str] = []
    try:
        with open(index_path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        log.debug("cannot read %r: %s", index_path, exc)
        return urls
    seen = set()
    for match in _HTTP_URL_RE.finditer(blob):
        try:
            url = match.group(0).decode("latin-1", "replace").strip()
        except Exception:  # noqa: BLE001
            continue
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _safe_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m lib.opera_cache <opera-cache-dir>", file=sys.stderr)
        raise SystemExit(2)

    target = sys.argv[1]
    if not detect(target):
        print("not a recognizable Opera cache: %s" % target, file=sys.stderr)
    count = 0
    for e in parse(target):
        count += 1
        loc = e.source_path if e.source_path else (
            "%d bytes" % len(e.data) if e.data is not None else "(no-body)"
        )
        print("%s %s" % (e.url or "(no-url)", loc))
    print("--- %d entries ---" % count, file=sys.stderr)
