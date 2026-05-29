"""Loading of Decache's lost-media database and auxiliary data files.

These data files are distributed separately from the program (they are
``.gitignore``-d) and live under ``bin/data/``.  The loader degrades gracefully
when a file is missing so the program can still run its magic-byte fallback
scan.

``video_data.txt`` is the heart of the database.  Each line is a quoted,
pipe-delimited record::

    "Title+with+pluses|id1,id2|<phash16hex>|<dur_min>|<dur_max>|...trailing..."

Fields
------
0. Title, with ``+`` standing in for spaces (and a few other substitutions the
   original used; see :func:`clean_title`).
1. One or more video ids, comma separated.  Ids are either 11-char YouTube ids
   or 16-hex-digit internal ids.
2. The earliest-known perceptual hash as 16 hex digits, or
   ``0000000000000000`` when no frame hash is known.
3. Minimum duration of the video.
4. Maximum duration of the video.

The original stored the duration bounds in several redundant encodings across
trailing columns (IEEE-754 doubles in seconds, the same in milliseconds, and
integer deciseconds).  We only need one, so :func:`_to_seconds` auto-detects the
encoding of fields 3 and 4 and normalises both to float seconds.
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("decache.database")

# Regexes (kept identical in spirit to the originals in read_cache.bat).
# Pull a video id out of a cache/watch URL.
URL_ID_PATTERN = r"[?&](?:video_id|id|v)=([\w-]{11}(?![\w-])|[0-9a-f]{16}(?![0-9a-f]))"
# Pull an 11-char id out of a history "watch?v=" URL.
HISTORY_ID_PATTERN = r"[?&]v=([\w-]{11})"


@dataclass
class VideoRecord:
    """One lost-media entry from ``video_data.txt``."""

    title: str
    ids: List[str]
    phash: str
    dur_min: Optional[float]
    dur_max: Optional[float]
    raw: str = ""

    @property
    def has_phash(self) -> bool:
        return bool(self.phash) and self.phash != "0000000000000000"

    def duration_matches(self, seconds: float, tolerance: float = 0.0) -> bool:
        """Whether a measured duration falls in this record's range."""
        if self.dur_min is None or self.dur_max is None:
            return False
        return (self.dur_min - tolerance) <= seconds <= (self.dur_max + tolerance)


# The fixed file-check globs start_decache.bat always searches for (before the
# unique_names suffixes are appended).  ``+`` is the original wildcard marker.
BASE_FILE_CHECKS = ["get_video+", "videoplayback+", "+.flv", "+.on2", "+.webm", "+.mp4"]


@dataclass
class Database:
    """All loaded data files plus indexes for fast lookup."""

    videos: List[VideoRecord] = field(default_factory=list)
    by_id: Dict[str, VideoRecord] = field(default_factory=dict)
    # IE "unique filename" globs appended to the file-check list (e.g. *.swf).
    unique_globs: List[str] = field(default_factory=list)
    # The full video file-check globs (get_video*, *.webm, ...).
    video_globs: List[str] = field(default_factory=list)
    # YouTube video ids whose watch pages reveal o- asset ids.
    watch_page_ids: List[str] = field(default_factory=list)
    # findstr terms that mark a history URL as interesting (legacy; unused by
    # the SQLite history parser, kept for completeness).
    history_data: List[str] = field(default_factory=list)
    # Substrings/domains identifying recoverable assets in a cache URL.
    asset_terms: List[str] = field(default_factory=list)
    data_dir: str = ""

    def lookup_id(self, video_id: str) -> Optional[VideoRecord]:
        return self.by_id.get(video_id)

    def records_in_duration(self, seconds: float, tolerance: float = 0.0) -> List[VideoRecord]:
        return [v for v in self.videos if v.duration_matches(seconds, tolerance)]


def _glob_from_token(token: str) -> str:
    """Convert a ``+``-wildcard data token into an fnmatch glob."""
    return token.strip().replace("+", "*")


def _to_seconds(token: str) -> Optional[float]:
    """Decode a duration field to float seconds.

    Handles the encodings seen across versions of ``video_data.txt``:

    * an ``HH:MM:SS.ss`` (or ``MM:SS`` / ``SS``) time string -- the current
      format, matching ffmpeg's ``Duration:`` output;
    * a 16-hex-digit IEEE-754 double (older format); and
    * a plain decimal number of seconds.
    """
    token = token.strip()
    if not token or token.lower() == "x":
        return None
    # Time string "HH:MM:SS.ss" / "MM:SS" / "SS.ss".
    if ":" in token:
        parts = token.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for n in nums:
            seconds = seconds * 60 + n
        return seconds
    # 16 hex digits -> IEEE-754 double (big-endian bit pattern).
    if len(token) == 16 and all(c in "0123456789abcdefABCDEF" for c in token):
        try:
            return struct.unpack(">d", bytes.fromhex(token))[0]
        except (ValueError, struct.error):
            pass
    # Plain number.
    try:
        return float(token)
    except ValueError:
        return None


def clean_title(title: str) -> str:
    """Reverse the title encoding used in the database / filenames.

    Mirrors the substitutions in start_decache.bat's :cleanseTitle /
    :printFinding (``+`` -> space, ``PLUSER`` -> ``+``, braces -> parens, etc.).
    """
    out = title
    out = out.replace("PLUSER", "\x00")  # protect literal plus markers
    out = out.replace("+", " ")
    out = out.replace("\x00", "+")
    out = out.replace("{", "(").replace("}", ")")
    return out


def parse_video_line(line: str) -> Optional[VideoRecord]:
    line = line.strip()
    if not line:
        return None
    # Records are wrapped in double quotes.
    if line.startswith('"') and line.endswith('"'):
        line = line[1:-1]
    parts = line.split("|")
    if len(parts) < 3:
        log.debug("skipping malformed video_data line: %r", line[:80])
        return None
    title = parts[0]
    ids = [i for i in parts[1].split(",") if i]
    phash = parts[2].strip().lower() if len(parts) > 2 else "0000000000000000"
    dur_min = _to_seconds(parts[3]) if len(parts) > 3 else None
    dur_max = _to_seconds(parts[4]) if len(parts) > 4 else None
    if dur_min is not None and dur_max is not None and dur_min > dur_max:
        dur_min, dur_max = dur_max, dur_min
    return VideoRecord(title=title, ids=ids, phash=phash,
                       dur_min=dur_min, dur_max=dur_max, raw=line)


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh]
    except OSError:
        return []


def load_database(data_dir: str) -> Database:
    """Load every data file present under ``data_dir`` (``bin/data``)."""
    db = Database(data_dir=data_dir)

    vpath = os.path.join(data_dir, "video_data.txt")
    for line in _read_lines(vpath):
        rec = parse_video_line(line)
        if rec is None:
            continue
        db.videos.append(rec)
        for vid in rec.ids:
            db.by_id.setdefault(vid, rec)
    if db.videos:
        log.info("loaded %d lost-media records from %s", len(db.videos), vpath)
    else:
        log.warning("no video_data.txt found at %s; running fallback scan only", vpath)

    # unique_names.txt is a single comma-separated line of +-wildcard tokens
    # (e.g. ",mainpage_final+.swf,marioGolf+.swf,toon0+.dcr").
    unique_raw = " ".join(_read_lines(os.path.join(data_dir, "unique_names.txt")))
    db.unique_globs = [_glob_from_token(t) for t in unique_raw.split(",") if t.strip()]
    db.video_globs = [_glob_from_token(t) for t in BASE_FILE_CHECKS]

    # watch_page_data.txt: one 11-char YouTube id per line.
    db.watch_page_ids = [ln.strip() for ln in
                         _read_lines(os.path.join(data_dir, "watch_page_data.txt"))
                         if ln.strip()]

    db.history_data = [ln for ln in _read_lines(os.path.join(data_dir, "history_data.txt")) if ln]

    # asset_data.txt: URL/domain search terms (plus stray NirSoft column labels
    # we filter out by requiring a "." or "/").
    asset_terms = []
    for ln in _read_lines(os.path.join(data_dir, "asset_data.txt")):
        t = ln.strip().lower()
        if t and ("." in t or "/" in t):
            asset_terms.append(t)
    db.asset_terms = asset_terms

    if db.videos or db.asset_terms:
        log.info("data: %d videos, %d asset terms, %d watch-page ids, %d unique globs",
                 len(db.videos), len(db.asset_terms), len(db.watch_page_ids),
                 len(db.unique_globs))
    return db
