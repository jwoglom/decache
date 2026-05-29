"""Internet Explorer / WinINet "Temporary Internet Files" cache parser.

This recovers entries from the classic MSIE disk cache, whose index lives in a
binary ``index.dat`` (the WinINet "URL cache MMF"), typically at::

    Temporary Internet Files/Content.IE5/index.dat
    Temporary Internet Files/Content.IE5/<RANDOM>/   (the actual cached files)

Format overview
---------------
``index.dat`` begins with a 28-byte ASCII signature
``"Client UrlCache MMF Ver 5.2"`` (the version digits vary).  Following the
header the file is divided into 0x80-byte blocks; each cache record starts on a
block boundary with a 4-byte ASCII tag: ``"URL "``, ``"REDR"``, ``"LEAK"`` or
``"HASH"``.  We only care about ``"URL "`` records, which describe one cached
resource:

    +0x00  "URL " signature (4 bytes)
    +0x04  uint32  number of 0x80 blocks this record occupies
    +0x08  FILETIME (uint64) last-modified
    +0x10  FILETIME (uint64) last-accessed
    +0x3C  uint32  offset (within record) to the local filename ASCII string
    +0x44  uint32  offset (within record) to the URL ASCII string

FILETIMEs are 100-ns ticks since 1601; convert with
``posix = filetime / 1e7 - 11644473600``.

The cached body itself is a separate file living in a subdirectory next to
``index.dat``; the record's filename field gives its basename, which we resolve
by searching the index's parent tree.  When the body can't be located the entry
is still yielded URL-only (``source_path=None``, ``data=None``).

Pure standard library; best-effort and defensive -- a malformed record is
logged and skipped, never raised.
"""

from __future__ import annotations

import logging
import os
import struct
from typing import Dict, Iterator, Optional

from .cache_common import CacheEntry

log = logging.getLogger("decache.ie")

# WinINet index.dat constants.
_SIG_PREFIX = b"Client UrlCache MMF"
_SIG_LEN = 28
_BLOCK_SIZE = 0x80
# The header proper is 0x250 bytes; records live after it (after the directory
# table that follows).  We scan from here on block boundaries.
_HEADER_SIZE = 0x250

# FILETIME epoch difference: seconds between 1601-01-01 and 1970-01-01.
_FILETIME_EPOCH_DELTA = 11644473600.0

# Record tag we care about, plus the others we recognise (and skip over).
_TAG_URL = b"URL "
_KNOWN_TAGS = (b"URL ", b"REDR", b"LEAK", b"HASH")

# URL-record field offsets (relative to record start).
_OFF_BLOCKS = 0x04
_OFF_LASTMOD = 0x08
_OFF_LASTACC = 0x10
_OFF_FILENAME_PTR = 0x3C
_OFF_URL_PTR = 0x44


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def detect(path: str) -> bool:
    """Return ``True`` if *path* points at a WinINet IE cache.

    Accepts being handed the ``index.dat`` file directly, a directory that
    contains one, or a directory containing ``Content.IE5/index.dat``.  The
    decision is confirmed by the ``"Client UrlCache MMF"`` magic.
    """
    try:
        idx = _find_index_dat(path)
        if idx is None:
            return False
        return _has_signature(idx)
    except OSError as exc:
        log.debug("detect(%r) failed: %s", path, exc)
        return False


def _find_index_dat(path: str) -> Optional[str]:
    """Resolve *path* to an ``index.dat`` file, or ``None``."""
    if not path:
        return None
    if os.path.isfile(path):
        # Handed the index.dat (or some file) directly.
        return path
    if not os.path.isdir(path):
        return None
    candidates = (
        os.path.join(path, "index.dat"),
        os.path.join(path, "Content.IE5", "index.dat"),
        os.path.join(path, "Temporary Internet Files", "Content.IE5", "index.dat"),
    )
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def _has_signature(index_path: str) -> bool:
    """Check the ``"Client UrlCache MMF"`` magic at the start of the file."""
    try:
        with open(index_path, "rb") as fh:
            head = fh.read(len(_SIG_PREFIX))
        return head.startswith(_SIG_PREFIX)
    except OSError as exc:
        log.debug("cannot read %r: %s", index_path, exc)
        return False


# --------------------------------------------------------------------------- #
# Public parse entry point
# --------------------------------------------------------------------------- #
def parse(path: str) -> Iterator[CacheEntry]:
    """Yield :class:`CacheEntry` objects recovered from an IE cache.

    *path* may be the ``index.dat`` file or a directory containing it.  Each
    URL record is parsed best-effort; failures are logged and skipped.
    """
    try:
        index_path = _find_index_dat(path)
        if index_path is None or not _has_signature(index_path):
            log.debug("parse(%r): no IE index.dat with valid signature", path)
            return
    except OSError as exc:
        log.debug("parse(%r): %s", path, exc)
        return

    try:
        with open(index_path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        log.debug("cannot read %r: %s", index_path, exc)
        return

    n = len(blob)
    if n < _SIG_LEN:
        return

    # Validate header-declared file size loosely (don't trust it blindly).
    try:
        (declared_size,) = struct.unpack_from("<I", blob, 0x1C)
        if declared_size and declared_size <= n:
            blob = blob[:declared_size]
            n = declared_size
    except struct.error:
        pass

    # The cached bodies live in subdirectories next to index.dat; build a
    # basename -> full-path index of the parent tree once, lazily.
    file_index: Optional[Dict[str, str]] = None
    cache_root = os.path.dirname(os.path.abspath(index_path))

    offset = _HEADER_SIZE
    # Align scan start to a block boundary.
    if offset % _BLOCK_SIZE:
        offset += _BLOCK_SIZE - (offset % _BLOCK_SIZE)

    while offset + _BLOCK_SIZE <= n:
        tag = blob[offset : offset + 4]
        if tag not in _KNOWN_TAGS:
            offset += _BLOCK_SIZE
            continue

        # Determine how many blocks this record claims (used to skip ahead).
        rec_blocks = 1
        try:
            (rec_blocks_raw,) = struct.unpack_from("<I", blob, offset + _OFF_BLOCKS)
            if 1 <= rec_blocks_raw <= (n // _BLOCK_SIZE):
                rec_blocks = rec_blocks_raw
        except struct.error:
            rec_blocks = 1

        if tag == _TAG_URL:
            try:
                if file_index is None:
                    file_index = _build_file_index(cache_root)
                entry = _parse_url_record(blob, offset, rec_blocks, n, file_index)
                if entry is not None:
                    yield entry
            except Exception as exc:  # noqa: BLE001 - best-effort per record
                log.debug("URL record at 0x%X failed: %s", offset, exc)

        offset += rec_blocks * _BLOCK_SIZE


# --------------------------------------------------------------------------- #
# URL record parsing
# --------------------------------------------------------------------------- #
def _parse_url_record(
    blob: bytes,
    rec_off: int,
    rec_blocks: int,
    file_size: int,
    file_index: Dict[str, str],
) -> Optional[CacheEntry]:
    """Parse a single ``"URL "`` record into a :class:`CacheEntry`."""
    rec_end = min(rec_off + rec_blocks * _BLOCK_SIZE, file_size)
    rec_len = rec_end - rec_off
    if rec_len < 0x48:
        return None

    # Timestamps (FILETIME, little-endian uint64).
    last_modified = _filetime_at(blob, rec_off + _OFF_LASTMOD, file_size)
    last_accessed = _filetime_at(blob, rec_off + _OFF_LASTACC, file_size)

    # URL string pointer (relative to record start).
    url = ""
    try:
        (url_ptr,) = struct.unpack_from("<I", blob, rec_off + _OFF_URL_PTR)
        if 0 < url_ptr < rec_len:
            url = _read_cstring(blob, rec_off + url_ptr, file_size)
    except struct.error:
        pass

    # Local filename pointer (relative to record start); 0 == none.
    local_name = ""
    try:
        (fn_ptr,) = struct.unpack_from("<I", blob, rec_off + _OFF_FILENAME_PTR)
        if 0 < fn_ptr < rec_len:
            local_name = _read_cstring(blob, rec_off + fn_ptr, file_size)
    except struct.error:
        pass

    if not url and not local_name:
        return None

    entry = CacheEntry(backend="ie", url=url)
    entry.last_modified = last_modified
    entry.accessed = last_accessed

    if local_name:
        base = os.path.basename(local_name.replace("\\", "/"))
        entry.filename = base or None
        found = file_index.get(base.lower()) if base else None
        if found:
            entry.source_path = found
            # Prefer the record's last-modified; fall back to the file's mtime.
            if entry.last_modified is None:
                entry.last_modified = _safe_mtime(found)
        else:
            log.debug("cached body %r not found on disk for url %r", base, url)
    else:
        entry.filename = _filename_from_url(url)

    return entry


def _filetime_at(blob: bytes, off: int, file_size: int) -> Optional[float]:
    """Read a little-endian FILETIME at *off* and convert to POSIX, or None."""
    if off < 0 or off + 8 > file_size:
        return None
    try:
        (ft,) = struct.unpack_from("<Q", blob, off)
    except struct.error:
        return None
    if ft == 0:
        return None
    posix = ft / 1e7 - _FILETIME_EPOCH_DELTA
    # Reject absurd values (pre-1980 / far future) that signal garbage.
    if posix < 315532800 or posix > 4102444800:  # 1980..2100
        return None
    return posix


def _read_cstring(blob: bytes, off: int, file_size: int) -> str:
    """Read a NUL-terminated ASCII string at *off*, bounded by *file_size*."""
    if off < 0 or off >= file_size:
        return ""
    end = blob.find(b"\x00", off, file_size)
    if end == -1:
        end = file_size
    raw = blob[off:end]
    return raw.decode("latin-1", "replace").strip()


# --------------------------------------------------------------------------- #
# On-disk cached-body index
# --------------------------------------------------------------------------- #
def _build_file_index(root: str) -> Dict[str, str]:
    """Map lowercased basename -> full path for files under *root*.

    Walks the parent tree of ``index.dat`` so a record's local filename can be
    resolved to the actual cached body, which IE stores in randomly named
    subdirectories alongside the index.
    """
    index: Dict[str, str] = {}
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower() in ("index.dat",):
                    continue
                # First writer wins; duplicate basenames across subdirs are rare
                # and ambiguous either way.
                index.setdefault(name.lower(), os.path.join(dirpath, name))
    except OSError as exc:
        log.debug("walk of %r failed: %s", root, exc)
    return index


def _filename_from_url(url: str) -> Optional[str]:
    """Suggest a filename from the URL's path basename."""
    if not url:
        return None
    path = url.split("#", 1)[0].split("?", 1)[0]
    after_scheme = path.split("://", 1)[-1]
    base = after_scheme.rsplit("/", 1)[-1] if "/" in after_scheme else ""
    return base or None


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
        print("usage: python -m lib.ie_cache <index.dat-or-cache-dir>", file=sys.stderr)
        raise SystemExit(2)

    target = sys.argv[1]
    if not detect(target):
        print("not a recognizable IE cache: %s" % target, file=sys.stderr)
    count = 0
    for e in parse(target):
        count += 1
        loc = e.source_path if e.source_path else (
            "%d bytes" % len(e.data) if e.data is not None else "(no-body)"
        )
        print("%s %s" % (e.url or "(no-url)", loc))
    print("--- %d entries ---" % count, file=sys.stderr)
