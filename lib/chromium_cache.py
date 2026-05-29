"""Parser for the on-disk Chromium / Chrome / Edge / Opera-Blink HTTP cache.

Chromium ships two completely different on-disk cache backends and this module
supports both:

* **Simple cache** -- a directory (commonly ``Cache_Data``) holding one file
  per cached resource (``<16-hex>_0``) plus ``the-real-index`` /
  ``index-dir``.  Each entry file carries a ``SimpleFileHeader``, the key
  (URL), the body (stream 0) and the HTTP response headers (stream 1), each
  stream terminated by a ``SimpleFileEOF`` record.

* **Block-file cache** -- the older backend made of ``index`` plus
  ``data_0`` .. ``data_3`` block files and ``f_XXXXXX`` external files for
  large payloads.  The index points at ``EntryStore`` records which point at
  the per-stream data via packed ``CacheAddr`` values.

The public surface is :func:`detect` and :func:`parse`; the latter is a
best-effort generator that logs and skips corrupt entries rather than raising.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import struct
import zlib
from typing import Iterator, Optional, Tuple

from .cache_common import CacheEntry

log = logging.getLogger("decache.chromium")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Simple cache magics (little-endian uint64).
SIMPLE_FILE_MAGIC = 0xFCFB6D1BA7725C30
SIMPLE_EOF_MAGIC = 0xF4FA6F45970D41D8

# struct format for SimpleFileHeader: magic(u64) version(u32) key_len(u32)
# key_hash(u32).
_SIMPLE_HEADER = struct.Struct("<QIII")
_SIMPLE_HEADER_SIZE = _SIMPLE_HEADER.size  # 20

# SimpleFileEOF: magic(u64) flags(u32) crc32(u32) stream_size(u64).
_SIMPLE_EOF = struct.Struct("<QIIQ")
_SIMPLE_EOF_SIZE = _SIMPLE_EOF.size  # 24

# Block-file index magic (little-endian uint32) at offset 0.
BLOCK_INDEX_MAGIC = 0xC103CAC3
# Where the CacheAddr table starts inside the index file.
BLOCK_INDEX_TABLE_OFFSET = 0x9A0
# Block files reserve an 8KB header before their block array.
BLOCK_HEADER_SIZE = 0x2000

# Block sizes per data_n file selector (index into this table is file_type-1
# in the block-file numbering, but we resolve it directly from file_type
# below).  data_0=36, data_1=256, data_2=1024, data_3=4096.
_BLOCK_SIZES = {0: 36, 1: 256, 2: 1024, 3: 4096}

# Chrome/Windows epoch offset: seconds between 1601-01-01 and 1970-01-01.
_WIN_EPOCH_OFFSET = 11644473600.0

_SIMPLE_NAME_RE = re.compile(r"^[0-9a-f]{16}_0$")
_HEX16_RE = re.compile(r"[0-9a-f]{16}_0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chrome_time_to_posix(win_microseconds: int) -> Optional[float]:
    """Convert a Chrome/Windows timestamp (us since 1601) to POSIX seconds."""
    if not win_microseconds:
        return None
    try:
        return win_microseconds / 1e6 - _WIN_EPOCH_OFFSET
    except (TypeError, ValueError):
        return None


def _decompress(body: bytes, encoding: Optional[str]) -> Tuple[bytes, Optional[str]]:
    """Decompress ``body`` per Content-Encoding.

    Returns ``(decompressed_bytes, leftover_encoding)``.  The leftover encoding
    is ``None`` when decompression succeeded, or the original encoding string
    when we could not handle it (e.g. brotli without the optional module), so
    the caller can record it on the entry.
    """
    if not encoding or not body:
        return body, None
    enc = encoding.strip().lower()
    try:
        if enc == "gzip":
            return gzip.decompress(body), None
        if enc in ("deflate", "zlib"):
            try:
                return zlib.decompress(body), None
            except zlib.error:
                # Raw deflate stream (no zlib header).
                return zlib.decompress(body, -zlib.MAX_WBITS), None
        if enc == "br":
            try:
                import brotli  # type: ignore
            except ImportError:
                log.debug("brotli module unavailable; leaving body compressed")
                return body, "br"
            return brotli.decompress(body), None
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("failed to decompress %s body: %s", enc, exc)
        return body, enc
    # Unknown / identity encoding: leave as-is.
    return body, None


def _parse_http_headers(blob: bytes) -> Tuple[Optional[str], Optional[float]]:
    """Pull Content-Encoding and Last-Modified from a header block.

    The block is a run of NUL-separated strings ("HTTP/1.1 200 OK",
    "Header: value", ...).  We are tolerant of surrounding pickle framing by
    just scanning for printable header lines.
    """
    content_encoding: Optional[str] = None
    last_modified: Optional[float] = None
    try:
        parts = blob.split(b"\x00")
        for part in parts:
            if b":" not in part:
                continue
            try:
                name, _, value = part.partition(b":")
                name_s = name.decode("latin-1", "replace").strip().lower()
                value_s = value.decode("latin-1", "replace").strip()
            except Exception:  # noqa: BLE001
                continue
            if name_s == "content-encoding" and value_s:
                content_encoding = value_s
            elif name_s == "last-modified" and value_s:
                last_modified = _parse_http_date(value_s)
    except Exception as exc:  # noqa: BLE001
        log.debug("header parse failed: %s", exc)
    return content_encoding, last_modified


def _parse_http_date(value: str) -> Optional[float]:
    """Parse an HTTP-date header into a POSIX timestamp."""
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def _url_basename(url: str) -> Optional[str]:
    """Return a filename hint from the URL path, if any."""
    if not url:
        return None
    try:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        name = os.path.basename(path)
        return name or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect(path: str) -> bool:
    """Return True if ``path`` looks like a Chromium cache directory."""
    if not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
    except OSError:
        return False
    name_set = set(names)

    if "the-real-index" in name_set:
        return True
    if "data_0" in name_set or "data_1" in name_set:
        return True
    for name in names:
        if _SIMPLE_NAME_RE.match(name):
            return True

    index_path = os.path.join(path, "index")
    if os.path.isfile(index_path):
        try:
            with open(index_path, "rb") as fh:
                head = fh.read(4)
            if len(head) == 4 and struct.unpack("<I", head)[0] == BLOCK_INDEX_MAGIC:
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------

def parse(path: str) -> Iterator[CacheEntry]:
    """Yield one :class:`CacheEntry` per cached resource (best effort)."""
    if not os.path.isdir(path):
        log.warning("not a directory: %s", path)
        return

    try:
        names = set(os.listdir(path))
    except OSError as exc:
        log.warning("cannot list %s: %s", path, exc)
        return

    is_block = (
        struct.pack("<I", BLOCK_INDEX_MAGIC) and _is_block_index(path, names)
    )

    if is_block:
        yield from _parse_block_cache(path)
    else:
        yield from _parse_simple_cache(path, names)


def _is_block_index(path: str, names: set) -> bool:
    """True when this directory uses the block-file backend."""
    if "data_0" in names or "data_1" in names:
        return True
    index_path = os.path.join(path, "index")
    if os.path.isfile(index_path):
        try:
            with open(index_path, "rb") as fh:
                head = fh.read(4)
            return len(head) == 4 and struct.unpack("<I", head)[0] == BLOCK_INDEX_MAGIC
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Simple cache
# ---------------------------------------------------------------------------

def _parse_simple_cache(path: str, names: set) -> Iterator[CacheEntry]:
    """Parse a simple-cache directory file by file."""
    for name in sorted(names):
        if not _SIMPLE_NAME_RE.match(name):
            continue
        entry_path = os.path.join(path, name)
        try:
            entry = _parse_simple_entry(entry_path)
        except Exception as exc:  # noqa: BLE001 - never raise on corruption
            log.warning("simple entry %s failed: %s", entry_path, exc)
            continue
        if entry is not None and entry.data:
            yield entry


def _parse_simple_entry(entry_path: str) -> Optional[CacheEntry]:
    """Parse a single ``<hash>_0`` simple-cache file."""
    with open(entry_path, "rb") as fh:
        raw = fh.read()
    if len(raw) < _SIMPLE_HEADER_SIZE:
        return None

    magic, version, key_len, _key_hash = _SIMPLE_HEADER.unpack_from(raw, 0)
    if magic != SIMPLE_FILE_MAGIC:
        log.debug("bad simple magic in %s", entry_path)
        return None

    key_start = _SIMPLE_HEADER_SIZE
    key_end = key_start + key_len
    if key_end > len(raw):
        log.debug("key length overruns file %s", entry_path)
        return None
    url = raw[key_start:key_end].decode("utf-8", "replace")

    # Stream 0 (body) starts right after the key.  Each stream is followed by a
    # SimpleFileEOF record.  We locate EOF records to learn stream sizes.
    body_start = key_end

    # Find the first EOF record at/after body_start -- it terminates stream 0
    # and its stream_size is the body length.
    eof_off, eof = _find_simple_eof(raw, body_start)
    if eof is None or eof_off is None:
        # Fall back: treat everything after the key (minus a trailing EOF) as
        # the body if we can't locate the record.
        log.debug("no stream-0 EOF in %s; using heuristic", entry_path)
        body = raw[body_start:]
        stream1 = b""
    else:
        _emagic, _flags, _crc, stream_size = eof
        body_end = body_start + stream_size
        if body_end > len(raw):
            body_end = eof_off
        body = raw[body_start:body_end]
        # Stream 1 (HTTP headers) sits after this EOF record; grab the bytes
        # up to the next EOF record (or end of file) for header parsing.
        stream1_start = eof_off + _SIMPLE_EOF_SIZE
        next_eof_off, _next_eof = _find_simple_eof(raw, stream1_start)
        if next_eof_off is not None:
            stream1 = raw[stream1_start:next_eof_off]
        else:
            stream1 = raw[stream1_start:]

    content_encoding, last_modified = _parse_http_headers(stream1)

    body, leftover_enc = _decompress(body, content_encoding)
    if not body:
        return None

    if last_modified is None:
        try:
            last_modified = os.path.getmtime(entry_path)
        except OSError:
            last_modified = None

    return CacheEntry(
        url=url,
        data=body,
        source_path=entry_path,
        filename=_url_basename(url),
        last_modified=last_modified,
        created=last_modified,
        accessed=None,
        content_encoding=leftover_enc,
        backend="chromium",
    )


def _find_simple_eof(raw: bytes, search_from: int):
    """Locate the next SimpleFileEOF record at/after ``search_from``.

    Returns ``(offset, (magic, flags, crc, stream_size))`` or ``(None, None)``.
    """
    needle = struct.pack("<Q", SIMPLE_EOF_MAGIC)
    pos = raw.find(needle, search_from)
    while pos != -1:
        if pos + _SIMPLE_EOF_SIZE <= len(raw):
            rec = _SIMPLE_EOF.unpack_from(raw, pos)
            return pos, rec
        pos = raw.find(needle, pos + 1)
    return None, None


# ---------------------------------------------------------------------------
# Block-file cache
# ---------------------------------------------------------------------------

class _BlockCache:
    """Resolves CacheAddr values to bytes within a block-file cache dir."""

    def __init__(self, path: str):
        self.path = path
        self._data_files = {}  # selector -> bytes

    def _data_file(self, selector: int) -> Optional[bytes]:
        if selector in self._data_files:
            return self._data_files[selector]
        fpath = os.path.join(self.path, "data_%d" % selector)
        data = None
        try:
            with open(fpath, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            log.debug("cannot read %s: %s", fpath, exc)
        self._data_files[selector] = data
        return data

    def read_addr(self, addr: int, size: Optional[int] = None) -> bytes:
        """Resolve a CacheAddr to raw bytes (truncated to ``size`` if given)."""
        if not addr or not (addr & 0x80000000):
            return b""
        file_type = (addr >> 28) & 0x7

        if file_type == 0:
            # External file f_%06x; bits 0-27 hold the number.
            file_number = addr & 0x0FFFFFFF
            fpath = os.path.join(self.path, "f_%06x" % file_number)
            try:
                with open(fpath, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                log.debug("cannot read external %s: %s", fpath, exc)
                return b""
            return data[:size] if size is not None else data

        # Block file.  file_type 1=rankings, 2..5 map to data_0..data_3 in the
        # historical numbering; we read the selector from bits 24-27 which is
        # the data_n index directly.
        selector = (addr >> 24) & 0x0F
        block_size = _block_size_for_type(file_type)
        if block_size is None:
            log.debug("unknown block file_type %d (addr 0x%08x)", file_type, addr)
            return b""
        block_number = addr & 0xFFFF
        num_blocks = ((addr >> 16) & 0xFF) or 1

        data = self._data_file(selector)
        if data is None:
            return b""
        offset = BLOCK_HEADER_SIZE + block_number * block_size
        length = num_blocks * block_size
        chunk = data[offset:offset + length]
        return chunk[:size] if size is not None else chunk


def _block_size_for_type(file_type: int) -> Optional[int]:
    """Map a CacheAddr file_type to its block size.

    file_type 2..5 correspond to data files with block sizes 36, 256, 1024,
    4096 respectively (the classic Chromium kBlockHeaderSize table).
    """
    table = {1: 36, 2: 256, 3: 1024, 4: 4096, 5: 4096}
    return table.get(file_type)


# EntryStore layout (subset we need), all little-endian, starting at block top:
#   long      hash            (offset 0, u32 -- stored as 32-bit in practice)
#   CacheAddr next            (offset 4)
#   CacheAddr rankings_node   (offset 8)
#   int32 reuse_count         (offset 12)
#   int32 refetch_count       (offset 16)
#   int32 state               (offset 20)
#   uint64 creation_time      (offset 24)
#   int32 key_len             (offset 32)
#   CacheAddr long_key        (offset 36)
#   uint32 data_size[4]       (offset 40)
#   CacheAddr data_addr[4]    (offset 56)
#   uint32 flags              (offset 72)
#   ... pad ...
#   char key[]                (offset 0xA8 = 168)
_ENTRY_KEY_OFFSET = 0xA8
_ENTRY_FIXED = struct.Struct("<IIIiiiQiI4I4II")  # up to flags (offset 76)


def _parse_block_cache(path: str) -> Iterator[CacheEntry]:
    """Walk the block-file index table and yield entries."""
    index_path = os.path.join(path, "index")
    try:
        with open(index_path, "rb") as fh:
            index = fh.read()
    except OSError as exc:
        log.warning("cannot read block index %s: %s", index_path, exc)
        return

    if len(index) < 4 or struct.unpack_from("<I", index, 0)[0] != BLOCK_INDEX_MAGIC:
        log.warning("bad block index magic in %s", index_path)
        return

    cache = _BlockCache(path)

    table = index[BLOCK_INDEX_TABLE_OFFSET:]
    count = len(table) // 4
    seen = set()
    for i in range(count):
        addr = struct.unpack_from("<I", table, i * 4)[0]
        if not addr or not (addr & 0x80000000):
            continue
        # Follow the chain of entries hashing to this bucket.
        next_addr = addr
        guard = 0
        while next_addr and (next_addr & 0x80000000) and guard < 1024:
            guard += 1
            if next_addr in seen:
                break
            seen.add(next_addr)
            try:
                entry, chain_next = _parse_block_entry(cache, next_addr)
            except Exception as exc:  # noqa: BLE001 - never raise
                log.warning("block entry 0x%08x failed: %s", next_addr, exc)
                break
            if entry is not None and entry.data:
                yield entry
            next_addr = chain_next


def _parse_block_entry(cache: "_BlockCache", addr: int):
    """Parse one EntryStore at CacheAddr ``addr``.

    Returns ``(CacheEntry|None, next_addr)``.
    """
    block = cache.read_addr(addr)
    if len(block) < _ENTRY_FIXED.size:
        return None, 0

    (
        _hash,
        next_addr,
        _rankings,
        _reuse,
        _refetch,
        _state,
        creation_time,
        key_len,
        long_key,
        ds0, ds1, ds2, ds3,
        da0, da1, da2, da3,
        _flags,
    ) = _ENTRY_FIXED.unpack_from(block, 0)
    data_size = (ds0, ds1, ds2, ds3)
    data_addr = (da0, da1, da2, da3)

    # Recover the key (URL).
    url = _read_block_key(cache, block, key_len, long_key)

    # Stream 1 is the body; stream 0 is the HTTP headers.
    headers_blob = b""
    if data_addr[0] and data_size[0]:
        headers_blob = cache.read_addr(data_addr[0], data_size[0])
    content_encoding, last_modified = _parse_http_headers(headers_blob)

    body = b""
    if data_addr[1] and data_size[1]:
        body = cache.read_addr(data_addr[1], data_size[1])

    body, leftover_enc = _decompress(body, content_encoding)
    if not body:
        return None, next_addr

    created = _chrome_time_to_posix(creation_time)
    if last_modified is None:
        last_modified = created

    entry = CacheEntry(
        url=url,
        data=body,
        source_path=None,
        filename=_url_basename(url),
        last_modified=last_modified,
        created=created,
        accessed=None,
        content_encoding=leftover_enc,
        backend="chromium",
    )
    return entry, next_addr


def _read_block_key(cache: "_BlockCache", block: bytes, key_len: int, long_key: int) -> str:
    """Recover the URL key, inline at 0xA8 or via the ``long_key`` addr."""
    if key_len <= 0:
        return ""
    inline_end = _ENTRY_KEY_OFFSET + key_len
    if long_key and (long_key & 0x80000000):
        raw = cache.read_addr(long_key, key_len)
    elif inline_end <= len(block):
        raw = block[_ENTRY_KEY_OFFSET:inline_end]
    else:
        # Inline but truncated by the block we read -- take what we have.
        raw = block[_ENTRY_KEY_OFFSET:]
    # Keys may carry a trailing NUL.
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if len(sys.argv) < 2:
        print("usage: python -m lib.chromium_cache <cache-dir>", file=sys.stderr)
        sys.exit(2)

    cache_dir = sys.argv[1]
    print("detect(%s) = %s" % (cache_dir, detect(cache_dir)), file=sys.stderr)
    n = 0
    for ce in parse(cache_dir):
        n += 1
        print("%s %d bytes" % (ce.url, len(ce.data or b"")))
    print("total: %d entries" % n, file=sys.stderr)
