"""Video-container detection by magic bytes, plus fragment classification.

The original program read the first 17 bytes of every candidate file and matched
hex prefixes to decide whether it was the start of a video, a continuation
fragment, or junk.  This module reproduces that exactly so fragmented cache
files (Chrome/Firefox store media in ~1 MiB chunks) can be stitched back
together by the scanner.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

# Cache backends frequently split a media response into chunks of exactly this
# size; a chunk of this exact length means "more is coming", a shorter chunk is
# the tail of the resource.  (Mirrors the `%%~zg neq 1048576` test.)
CHUNK_SIZE = 1048576

# Number of header bytes the original inspected (perm_error.bat -> hex.vbs 17).
HEADER_BYTES = 17


class Kind(Enum):
    WEBM = "webm"            # EBML header -> start of a WebM/Matroska file
    FLV = "flv"             # "FLV" -> Flash video (Google Video era)
    MP4 = "mp4"             # ftyp box -> start of an MP4
    WEBM_FRAG = "webm_frag"  # Cluster element -> continuation of a WebM
    MP4_FRAG = "mp4_frag"    # moof/styp box -> continuation of a fragmented MP4
    OTHER = "other"          # not a recognised header (maybe a raw continuation)


def _hex(data: bytes, n: int = HEADER_BYTES) -> str:
    return data[:n].hex().upper()


def classify(data: bytes) -> Kind:
    """Classify a file by its leading bytes, matching the batch logic order."""
    h = _hex(data)
    if len(h) < 8:
        return Kind.OTHER

    first4 = h[0:8]
    first3 = h[0:6]
    bytes4_8 = h[8:16]   # bytes index 4..7
    bytes8_12 = h[16:24]  # bytes index 8..11

    # WebM / Matroska EBML header.
    if first4 == "1A45DFA3":
        return Kind.WEBM

    # FLV signature ("FLV").
    if first3 == "464C56":
        return Kind.FLV

    # MP4: starts 00 00 00 xx, has an "ftyp" box, but is not AVIF.
    if first3 == "000000" and bytes4_8 == "66747970" and bytes8_12 != "61766966":
        return Kind.MP4

    # WebM fragment: a Cluster element (continuation of a WebM stream).
    if first4 == "1F43B675":
        return Kind.WEBM_FRAG

    # MP4 fragment: a "moof" or "styp" box (continuation of fragmented MP4).
    if bytes4_8 in ("6D6F6F66", "73747970"):
        return Kind.MP4_FRAG

    return Kind.OTHER


def extension_for(kind: Kind) -> Optional[str]:
    """Default file extension to save a freshly-detected container under."""
    return {
        Kind.WEBM: ".webm",
        Kind.FLV: ".flv",
        Kind.MP4: ".mp4",
    }.get(kind)


def is_full_chunk(size: int) -> bool:
    """True if a chunk is exactly the cache chunk size (more may follow)."""
    return size == CHUNK_SIZE
