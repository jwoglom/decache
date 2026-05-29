"""Firefox disk-cache parser for Decache.

Supports both Firefox cache generations:

* **cache2** (Firefox 32+, the common case): a ``cache2/entries`` directory
  whose files are named by the uppercase hex SHA1 of the cache key.  Each file
  packs the response body, per-chunk hashes, a metadata block, and a trailing
  big-endian ``uint32`` pointing at the real body length / start of the
  metadata region.  This path is fully parsed: URL, headers, content-encoding,
  decompression (gzip/deflate via stdlib, brotli if ``brotli`` is importable),
  and timestamps.

* **legacy cache** (Firefox <32): a directory holding ``_CACHE_MAP_`` plus
  ``_CACHE_001_`` / ``_CACHE_002_`` / ``_CACHE_003_``.  The legacy block-file
  format is complex; this module implements a **best-effort** recovery only.
  It scans the ``_CACHE_00x_`` block files for embedded HTTP URLs and tries to
  associate each cache record's metadata with its data block (inline or in a
  separate ``m_XXXXXXXX`` file living in the cache root).  Entries recovered
  from the legacy format are not guaranteed complete.

Pure standard library; no third-party packages required (``brotli`` is used
opportunistically if installed but never mandatory).
"""

from __future__ import annotations

import gzip
import logging
import math
import os
import re
import struct
import zlib
from typing import Iterator, Optional

from .cache_common import CacheEntry

log = logging.getLogger("decache.firefox")

# cache2 chunk size: data is hashed in 256 KiB chunks, each with a 2-byte hash.
_CHUNK_SIZE = 256 * 1024

# A cache2 entry filename is the uppercase hex SHA1 of the key: 40 hex chars.
_SHA1_NAME_RE = re.compile(r"^[0-9A-F]{40}$")

# Legacy block-file names.
_LEGACY_BLOCK_RE = re.compile(r"^_CACHE_00[123]_$")

# Find an http(s) URL embedded in a byte blob (legacy best-effort scan).
_HTTP_URL_RE = re.compile(rb"https?://[^\x00-\x20\"'<>\\]+")


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def detect(path: str) -> bool:
    """Return ``True`` if *path* looks like a Firefox cache.

    Accepts a cache root containing ``cache2/entries``, an ``entries``
    directory directly (filled with 40-hex-char files), a ``cache2`` directory,
    or any directory holding ``_CACHE_MAP_`` (legacy format).
    """
    try:
        if not os.path.isdir(path):
            return False

        # cache root -> cache2/entries
        if os.path.isdir(os.path.join(path, "cache2", "entries")):
            return True

        # given the cache2 dir directly
        if os.path.isdir(os.path.join(path, "entries")) and os.path.basename(
            os.path.normpath(path)
        ) == "cache2":
            return True

        # legacy format marker
        if os.path.isfile(os.path.join(path, "_CACHE_MAP_")):
            return True

        # the entries dir itself: look for at least one 40-hex-char file
        for name in os.listdir(path):
            if _SHA1_NAME_RE.match(name):
                return True
    except OSError as exc:
        log.debug("detect(%r) failed: %s", path, exc)
    return False


# --------------------------------------------------------------------------- #
# Public parse entry point
# --------------------------------------------------------------------------- #
def parse(path: str) -> Iterator[CacheEntry]:
    """Yield :class:`CacheEntry` objects recovered from *path*.

    Best-effort: a failure parsing any single entry is logged and skipped, never
    raised.  *path* may be a cache root, a ``cache2`` directory, a ``cache2``
    ``entries`` directory, or a legacy cache directory.
    """
    entries_dir = _find_entries_dir(path)
    if entries_dir is not None:
        yield from _parse_cache2(entries_dir)
        return

    # Legacy format?
    if os.path.isdir(path) and os.path.isfile(os.path.join(path, "_CACHE_MAP_")):
        yield from _parse_legacy(path)
        return

    log.debug("parse(%r): no recognizable Firefox cache layout", path)


def _find_entries_dir(path: str) -> Optional[str]:
    """Resolve *path* to a cache2 ``entries`` directory, or ``None``."""
    try:
        if not os.path.isdir(path):
            return None

        # cache root -> cache2/entries
        cand = os.path.join(path, "cache2", "entries")
        if os.path.isdir(cand):
            return cand

        # cache2 dir -> entries
        cand = os.path.join(path, "entries")
        if os.path.isdir(cand):
            return cand

        # the entries dir itself
        base = os.path.basename(os.path.normpath(path))
        if base == "entries":
            return path
        for name in os.listdir(path):
            if _SHA1_NAME_RE.match(name):
                return path
    except OSError as exc:
        log.debug("_find_entries_dir(%r) failed: %s", path, exc)
    return None


# --------------------------------------------------------------------------- #
# cache2
# --------------------------------------------------------------------------- #
def _parse_cache2(entries_dir: str) -> Iterator[CacheEntry]:
    """Yield entries from a cache2 ``entries`` directory."""
    try:
        names = os.listdir(entries_dir)
    except OSError as exc:
        log.debug("cannot list %r: %s", entries_dir, exc)
        return

    for name in names:
        full = os.path.join(entries_dir, name)
        # Only regular files named like a SHA1 are cache entries; tolerate
        # other names by still trying, but skip obvious dirs.
        try:
            if not os.path.isfile(full):
                continue
            entry = _parse_cache2_file(full)
        except Exception as exc:  # noqa: BLE001 - best-effort per entry
            log.debug("failed to parse cache2 file %r: %s", full, exc)
            continue
        if entry is not None:
            yield entry


def _parse_cache2_file(path: str) -> Optional[CacheEntry]:
    """Parse one cache2 entry file into a :class:`CacheEntry`."""
    with open(path, "rb") as fh:
        blob = fh.read()
    n = len(blob)
    if n < 4:
        return None

    # Trailing uint32: offset of the real data end == real body length.
    (metadata_offset,) = struct.unpack(">I", blob[-4:])
    if metadata_offset > n or metadata_offset == 0:
        # Some entries (e.g. zero-length) won't have a sensible body; bail.
        log.debug("%s: implausible metadata_offset %d (n=%d)", path, metadata_offset, n)
        if metadata_offset > n:
            return None

    body = blob[:metadata_offset]

    # Metadata starts after the per-chunk hashes (2 bytes per 256 KiB chunk).
    num_chunks = math.ceil(metadata_offset / _CHUNK_SIZE) if metadata_offset else 0
    metadata_start = metadata_offset + num_chunks * 2

    meta = _parse_cache2_metadata(blob, metadata_start)

    file_mtime = _safe_mtime(path)

    entry = CacheEntry(backend="firefox", source_path=path)

    url = ""
    if meta is not None:
        url = _url_from_key(meta.get("key", ""))
        elements = meta.get("elements", {})
        resp_head = elements.get("response-head", "")
        content_encoding, last_modified_http = _parse_response_head(resp_head)

        # Decompress the body according to content-encoding.
        body, content_encoding = _decompress(body, content_encoding)
        entry.content_encoding = content_encoding

        # last_modified: metadata lastModified (unix secs) if nonzero, else mtime.
        lm = meta.get("lastModified", 0)
        if lm:
            entry.last_modified = float(lm)
        elif last_modified_http is not None:
            entry.last_modified = last_modified_http
        else:
            entry.last_modified = file_mtime

        lf = meta.get("lastFetched", 0)
        if lf:
            entry.accessed = float(lf)
    else:
        entry.last_modified = file_mtime

    entry.url = url
    entry.data = body
    entry.filename = _filename_from_url(url)
    return entry


class _Meta(dict):
    """Lightweight dict alias for cache2 metadata (for readability)."""


def _parse_cache2_metadata(blob: bytes, start: int) -> Optional[dict]:
    """Parse a CacheFileMetadata block beginning at *start*.

    Layout (all uint32 big-endian):
        version, fetchCount, lastFetched, lastModified, frecency,
        expirationTime, keySize, [flags (newer versions)]
    followed by the NUL-terminated key (keySize bytes) and a run of
    ``name\\0value\\0`` element pairs.
    """
    if start < 0 or start + 28 > len(blob):
        log.debug("metadata start %d out of range (len=%d)", start, len(blob))
        return None

    # The minimal header is 7 uint32s = 28 bytes; newer versions add a flags
    # uint32 (=32 bytes) before the key.
    try:
        (
            version,
            fetch_count,
            last_fetched,
            last_modified,
            frecency,
            expiration,
            key_size,
        ) = struct.unpack(">IIIIIII", blob[start : start + 28])
    except struct.error:
        return None

    header_len = 28
    # Firefox metadata version >= 2 carries a uint32 flags field after keySize.
    if version >= 2 and start + 32 <= len(blob):
        header_len = 32

    key_start = start + header_len
    key_end = key_start + key_size
    if key_size > len(blob) or key_end > len(blob):
        # Header may not include flags after all; retry without it.
        if header_len == 32:
            header_len = 28
            key_start = start + header_len
            key_end = key_start + key_size
    if key_size < 0 or key_end > len(blob):
        log.debug("metadata key_size %d implausible (len=%d)", key_size, len(blob))
        return None

    key_bytes = blob[key_start:key_end]
    # The key is NUL-terminated within its keySize span.
    key = key_bytes.split(b"\x00", 1)[0].decode("utf-8", "replace")

    # Elements follow the key: a sequence of NUL-terminated strings forming
    # name\0value\0 pairs, up to the end of the file (minus the trailing 4-byte
    # metadata-offset that we already consumed conceptually; it sits at the very
    # end, so stop before it).
    elem_blob = blob[key_end:-4] if len(blob) - key_end > 4 else b""
    elements = _parse_elements(elem_blob)

    return {
        "version": version,
        "fetchCount": fetch_count,
        "lastFetched": last_fetched,
        "lastModified": last_modified,
        "frecency": frecency,
        "expirationTime": expiration,
        "keySize": key_size,
        "key": key,
        "elements": elements,
    }


def _parse_elements(blob: bytes) -> dict:
    """Parse a run of ``name\\0value\\0`` pairs into a dict."""
    parts = blob.split(b"\x00")
    # Drop a trailing empty fragment caused by the final NUL.
    if parts and parts[-1] == b"":
        parts.pop()
    out: dict = {}
    for i in range(0, len(parts) - 1, 2):
        name = parts[i].decode("utf-8", "replace")
        value = parts[i + 1].decode("utf-8", "replace")
        if name:
            out[name] = value
    return out


# --------------------------------------------------------------------------- #
# Key / URL helpers
# --------------------------------------------------------------------------- #
def _url_from_key(key: str) -> str:
    """Extract the request URL from a cache2 key.

    Keys look like ``:https://host/path`` or carry an origin/partition prefix
    such as ``O^partitionKey=(...)^,:https://host/path`` or
    ``a,:https://...``.  We take the substring beginning at the first
    ``http`` occurrence, which is the actual URL.
    """
    if not key:
        return ""
    idx = key.find("http")
    if idx == -1:
        return ""
    url = key[idx:]
    # Keys can have trailing NULs already stripped; also stop at whitespace.
    url = url.split("\x00", 1)[0].strip()
    return url


def _filename_from_url(url: str) -> Optional[str]:
    """Suggest a filename from the URL path basename."""
    if not url:
        return None
    # Strip query/fragment, then take the last path segment.
    path = url.split("#", 1)[0].split("?", 1)[0]
    # Drop scheme://host
    after_scheme = path.split("://", 1)[-1]
    if "/" in after_scheme:
        base = after_scheme.rsplit("/", 1)[-1]
    else:
        base = ""
    return base or None


# --------------------------------------------------------------------------- #
# HTTP response-head parsing / decompression
# --------------------------------------------------------------------------- #
def _parse_response_head(resp_head: str) -> tuple[Optional[str], Optional[float]]:
    """Pull Content-Encoding and Last-Modified out of a raw response-head."""
    if not resp_head:
        return None, None
    content_encoding: Optional[str] = None
    last_modified: Optional[float] = None
    for line in resp_head.splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "content-encoding":
            content_encoding = value.lower() or None
        elif name == "last-modified":
            last_modified = _parse_http_date(value)
    return content_encoding, last_modified


def _parse_http_date(value: str) -> Optional[float]:
    """Parse an HTTP-date header into a POSIX timestamp, or ``None``."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug("bad HTTP date %r: %s", value, exc)
        return None


def _decompress(body: bytes, encoding: Optional[str]) -> tuple[bytes, Optional[str]]:
    """Decompress *body* per *encoding*; return (body, residual_encoding).

    On success the residual encoding is ``None``.  Brotli is attempted only if
    the ``brotli`` package is importable; otherwise the body is left compressed
    and ``"br"`` is returned so the caller knows.
    """
    if not encoding or not body:
        return body, None

    enc = encoding.lower().strip()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(body), None
        if enc == "deflate":
            # Try zlib (with header) first, then raw deflate.
            try:
                return zlib.decompress(body), None
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS), None
        if enc == "br":
            try:
                import brotli  # type: ignore

                return brotli.decompress(body), None
            except Exception as exc:  # noqa: BLE001
                log.debug("brotli unavailable/failed: %s", exc)
                return body, "br"
    except Exception as exc:  # noqa: BLE001 - decompression is best-effort
        log.debug("decompress(%s) failed: %s", enc, exc)
        return body, enc
    # Unknown / identity encoding: leave as-is.
    if enc in ("identity", ""):
        return body, None
    return body, enc


def _safe_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Legacy cache (Firefox <32) -- BEST EFFORT ONLY
# --------------------------------------------------------------------------- #
def _parse_legacy(cache_dir: str) -> Iterator[CacheEntry]:
    """Best-effort recovery from the legacy ``_CACHE_00x_`` block files.

    The legacy block-file format stores cache records (metadata: key URL,
    headers, timestamps) interleaved with data blocks.  A faithful parser would
    walk ``_CACHE_MAP_`` buckets and block-file allocation maps; this
    implementation instead scans each ``_CACHE_00x_`` file for embedded
    ``http(s)`` URLs to recover keys, and -- where a separate large-entry data
    file (``m_XXXXXXXX``) exists in the cache root -- yields an entry pointing
    at it via ``source_path``.

    Limitations: inline data blocks are not reliably sliced out, so most legacy
    entries are returned with ``data=None`` and only a recovered URL.  Use this
    as a discovery aid, not an exact extractor.
    """
    file_mtime_cache: dict = {}

    # Collect separate large-entry data files in the cache root, keyed by name.
    sep_files = {}
    try:
        for name in os.listdir(cache_dir):
            full = os.path.join(cache_dir, name)
            if name.startswith("m_") and os.path.isfile(full):
                sep_files[name] = full
    except OSError as exc:
        log.debug("cannot list legacy cache dir %r: %s", cache_dir, exc)

    seen_urls = set()

    for idx in ("1", "2", "3"):
        block_name = "_CACHE_00%s_" % idx
        block_path = os.path.join(cache_dir, block_name)
        if not os.path.isfile(block_path):
            continue
        try:
            with open(block_path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            log.debug("cannot read %r: %s", block_path, exc)
            continue

        mtime = file_mtime_cache.get(block_path)
        if mtime is None:
            mtime = _safe_mtime(block_path)
            file_mtime_cache[block_path] = mtime

        for match in _HTTP_URL_RE.finditer(blob):
            try:
                url = match.group(0).decode("ascii", "replace").rstrip("/")
                # The byte run may include a trailing key suffix; keep it simple.
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                entry = CacheEntry(
                    backend="firefox",
                    url=url,
                    source_path=block_path,
                    last_modified=mtime,
                    filename=_filename_from_url(url),
                )
                yield entry
            except Exception as exc:  # noqa: BLE001 - best-effort per record
                log.debug("legacy record scan failed in %r: %s", block_path, exc)
                continue

    # Surface separate large-entry data files directly so their bodies are
    # recoverable even when we couldn't tie them to a key.
    for name, full in sep_files.items():
        try:
            yield CacheEntry(
                backend="firefox",
                url="",
                source_path=full,
                last_modified=_safe_mtime(full),
                filename=name,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("legacy separate-file %r failed: %s", full, exc)
            continue


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m lib.firefox_cache <cache-path>", file=sys.stderr)
        raise SystemExit(2)

    target = sys.argv[1]
    if not detect(target):
        print("not a recognizable Firefox cache: %s" % target, file=sys.stderr)
    count = 0
    for e in parse(target):
        count += 1
        body_len = len(e.data) if e.data is not None else 0
        print("%s %d bytes" % (e.url or "(no-url)", body_len))
    print("--- %d entries ---" % count, file=sys.stderr)
