"""Shared data structures for Decache's browser-cache parsers.

Every cache backend (Chromium, Firefox, IE, Opera) yields a stream of
:class:`CacheEntry` objects.  The orchestrator only cares about three things:
the original request URL, the (decompressed) response body, and a timestamp it
can use for the history-window heuristics.  Backends fill in whatever extra
metadata they can.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CacheEntry:
    """A single item recovered from a browser cache.

    Attributes
    ----------
    url:
        The original request URL the resource was fetched from.  May be empty
        for fallback / index-less entries.
    data:
        The response body, already decompressed (gzip/brotli/deflate handled by
        the backend when it knows the content-encoding).  ``None`` means the
        backend could not read the body and ``source_path`` should be used
        instead.
    source_path:
        Absolute path to the on-disk file the body lives in, when the backend
        works by pointing at existing files rather than copying bytes out.
        Either ``data`` or ``source_path`` must be set.
    filename:
        A suggested filename hint (from the cache index), if any.
    last_modified / created / accessed:
        POSIX timestamps (float seconds) when known, else ``None``.  These feed
        the "video appeared in history within N minutes of the file's
        last-modified time" heuristic, so ``last_modified`` is the important
        one and the backend should set it to the cached file's mtime when no
        better value exists.
    content_encoding:
        Raw content-encoding string if the backend left the body compressed
        (rare; backends should decompress when possible).
    backend:
        Name of the backend that produced the entry, for logging.
    """

    url: str = ""
    data: Optional[bytes] = None
    source_path: Optional[str] = None
    filename: Optional[str] = None
    last_modified: Optional[float] = None
    created: Optional[float] = None
    accessed: Optional[float] = None
    content_encoding: Optional[str] = None
    backend: str = ""

    def read_bytes(self) -> bytes:
        """Return the entry body, reading from ``source_path`` if needed."""
        if self.data is not None:
            return self.data
        if self.source_path:
            with open(self.source_path, "rb") as fh:
                return fh.read()
        return b""

    def best_timestamp(self) -> Optional[float]:
        """Timestamp used for history-window matching (mtime preferred)."""
        return self.last_modified or self.created or self.accessed


@dataclass
class HistoryVisit:
    """A browser-history visit to a YouTube watch URL.

    ``video_id`` is the raw 11-char id pulled from ``?v=``; ``timestamp`` is a
    POSIX timestamp of the visit (used to bound when a cached file plausibly
    came from that video).
    """

    video_id: str
    timestamp: float


@dataclass
class CacheLocation:
    """A discovered cache directory plus the browser family it belongs to."""

    path: str
    family: str  # "chromium" | "firefox" | "ie" | "opera"
    history_files: list = field(default_factory=list)
