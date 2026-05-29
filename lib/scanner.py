"""Discovery of browser caches and history databases on a target tree.

The Windows original walked a fixed set of per-user AppData / "Local Settings"
paths.  Since a Linux port has to cope with arbitrary disk-image backups (any
layout, any OS the backup came from), we instead walk the whole target once and
identify caches structurally via each backend's ``detect()``.  Unreadable
directories are skipped (the equivalent of the original's permission handling),
logged, and never abort the scan.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Dict, Iterator, List, Tuple

from . import chromium_cache, firefox_cache, ie_cache, opera_cache
from .cache_common import CacheEntry, CacheLocation

log = logging.getLogger("decache.scanner")

# Ordered: a directory is attributed to the first backend that claims it, and we
# then prune it from further descent so nested index/entries dirs aren't
# re-detected.
BACKENDS: List[Tuple[str, Callable[[str], bool], Callable[[str], Iterator[CacheEntry]]]] = [
    ("chromium", chromium_cache.detect, chromium_cache.parse),
    ("firefox", firefox_cache.detect, firefox_cache.parse),
    ("ie", ie_cache.detect, ie_cache.parse),
    ("opera", opera_cache.detect, opera_cache.parse),
]

# Files we treat as browser history databases.
HISTORY_BASENAMES = {
    "history",           # Chrome/Chromium
    "archived history",  # Chrome/Chromium
    "places.sqlite",     # Firefox
    "history.dat",       # Firefox legacy (Mork)
}


def _on_walk_error(exc: OSError) -> None:
    log.debug("skipping unreadable path: %s", exc)


def discover(root: str) -> Tuple[List[CacheLocation], List[str]]:
    """Walk ``root`` and return (cache locations, history file paths)."""
    locations: List[CacheLocation] = []
    history_files: List[str] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_on_walk_error):
        # Collect history databases living in this directory.
        for name in filenames:
            if name.lower() in HISTORY_BASENAMES:
                history_files.append(os.path.join(dirpath, name))

        # Attribute the directory to a cache backend, if any.
        claimed = None
        for family, detect, _parse in BACKENDS:
            try:
                if detect(dirpath):
                    claimed = family
                    break
            except OSError as exc:
                log.debug("detect(%s) for %s failed: %s", family, dirpath, exc)
        if claimed:
            locations.append(CacheLocation(path=dirpath, family=claimed))
            # Don't descend into a recognised cache root; its internals
            # (entries/, data_*, Content.IE5/...) belong to this backend.
            dirnames[:] = []

    log.info("found %d cache location(s) and %d history file(s)",
             len(locations), len(history_files))
    for loc in locations:
        log.info("  cache [%s]: %s", loc.family, loc.path)
    for hf in history_files:
        log.info("  history: %s", hf)
    return locations, history_files


_PARSERS: Dict[str, Callable[[str], Iterator[CacheEntry]]] = {
    family: parse for family, _detect, parse in BACKENDS
}


def parse_location(loc: CacheLocation) -> Iterator[CacheEntry]:
    """Yield cache entries from a discovered location, never raising."""
    parser = _PARSERS.get(loc.family)
    if parser is None:
        return
    try:
        yield from parser(loc.path)
    except Exception as exc:  # noqa: BLE001 - best effort, keep scanning
        log.warning("parsing %s cache %s failed: %s", loc.family, loc.path, exc)
