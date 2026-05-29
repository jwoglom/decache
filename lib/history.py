"""Browser-history readers for Decache.

This module reads browser *history* databases (as opposed to caches) to find
out *when* the user visited a YouTube watch page.  The main tool correlates a
recovered cache file's timestamp with these visits to guess which video a blob
of cached bytes belongs to.

Three on-disk formats are understood:

* **Chrome / Chromium** ``History`` (and ``Archived History``) — a SQLite DB.
* **Firefox** ``places.sqlite`` — a SQLite DB.
* **Firefox legacy** ``history.dat`` — a Mork text database (best-effort only).

The public entry point is :func:`parse_history_file`, which sniffs the file
type and dispatches to the right parser.  Every function is defensive: they
never raise, returning ``[]`` (and logging) on any failure, because history
files are frequently locked, truncated, or copied out of partial backups.

Pure standard library only (``sqlite3`` is part of the stdlib).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

from .cache_common import HistoryVisit

log = logging.getLogger("decache.history")

# Chrome stores timestamps as microseconds since 1601-01-01 UTC.  This is the
# number of seconds between 1601-01-01 and the POSIX epoch (1970-01-01).
_CHROME_EPOCH_OFFSET = 11644473600

# Matches a YouTube watch URL's video id in a normal (un-escaped) URL string.
# Video ids are exactly 11 characters of [A-Za-z0-9_-].
_VIDEO_ID_RE = re.compile(r"[?&]v=([\w-]{11})")

# Matches "watch?v=<id>" anywhere in a Mork blob.  Mork escapes some bytes with
# a leading backslash (e.g. ``\)`` or hex like ``$2F``), so we tolerate stray
# backslashes immediately before the separators / id characters.
_MORK_WATCH_RE = re.compile(r"watch\\?\?\\?v\\?=([\w-]{11})")


def _video_id_from_url(url: str) -> str | None:
    """Return the 11-char YouTube video id in ``url`` if it is a watch URL."""
    if not url:
        return None
    if "youtube" not in url and "youtu.be" not in url:
        # ``v=`` can legitimately appear on non-YouTube URLs; restrict to hosts
        # that actually host watch pages to avoid false positives.
        return None
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def parse_history_file(path: str) -> list[HistoryVisit]:
    """Auto-detect the type of history file at ``path`` and parse it.

    Dispatch rules (in order):

    * name ends with ``.sqlite`` or contains ``places`` -> Firefox places.
    * basename is ``History`` / ``Archived History`` -> Chrome.
    * name is ``history.dat`` or content looks like Mork -> Mork.
    * otherwise sniff the first 16 bytes; a SQLite magic header is probed by
      reading ``sqlite_master`` to decide between Chrome (has ``urls``) and
      Firefox (has ``moz_places``).

    Never raises; returns ``[]`` on any failure.
    """
    try:
        name = os.path.basename(path)
        lname = name.lower()

        if lname.endswith(".sqlite") or "places" in lname:
            return parse_firefox_places(path)

        if name in ("History", "Archived History"):
            return parse_chrome(path)

        # Peek at the first bytes for format sniffing.
        header = b""
        try:
            with open(path, "rb") as fh:
                header = fh.read(64)
        except OSError as exc:
            log.warning("history: cannot read %s: %s", path, exc)
            return []

        if lname == "history.dat" or header.startswith(b"// <!-- <mdb:mork"):
            return parse_firefox_mork(path)

        if header.startswith(b"SQLite format 3\x00"):
            # Ambiguous SQLite file: inspect its tables to pick a parser.
            tables = _sqlite_table_names(path)
            if "urls" in tables:
                return parse_chrome(path)
            if "moz_places" in tables:
                return parse_firefox_places(path)
            log.warning(
                "history: %s is SQLite but has neither 'urls' nor "
                "'moz_places' (tables: %s)",
                path,
                sorted(tables),
            )
            return []

        log.warning("history: could not determine type of %s", path)
        return []
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.warning("history: unexpected error dispatching %s: %s", path, exc)
        return []


def _open_sqlite_resilient(path: str) -> tuple[sqlite3.Connection, str | None]:
    """Open a possibly-locked/partial SQLite DB read-only and resilient.

    Returns ``(connection, tmp_path)``.  ``tmp_path`` is the path of a temporary
    copy that the caller must delete after closing the connection (``None`` if
    we opened the original file directly via an immutable URI).

    Strategy: first try the ``immutable=1`` URI which lets sqlite read a file
    that another process holds open without taking locks or replaying the WAL.
    If that fails, fall back to copying the file to a temp location and opening
    the copy normally.
    """
    uri = "file:{}?immutable=1&mode=ro".format(_uri_quote(os.path.abspath(path)))
    try:
        conn = sqlite3.connect(uri, uri=True)
        # Force a cheap read to surface "file is not a database" early.
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        return conn, None
    except sqlite3.Error as exc:
        log.debug("history: immutable open of %s failed (%s); copying", path, exc)

    fd, tmp_path = tempfile.mkstemp(prefix="decache_hist_", suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copy(path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        return conn, tmp_path
    except (OSError, sqlite3.Error):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _uri_quote(path: str) -> str:
    """Percent-encode a filesystem path for use in a sqlite ``file:`` URI."""
    from urllib.parse import quote

    return quote(path)


def _sqlite_table_names(path: str) -> set[str]:
    """Return the set of table names in the SQLite DB at ``path`` (empty on error)."""
    conn = None
    tmp_path = None
    try:
        conn, tmp_path = _open_sqlite_resilient(path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    except (sqlite3.Error, OSError) as exc:
        log.warning("history: cannot list tables of %s: %s", path, exc)
        return set()
    finally:
        _cleanup_sqlite(conn, tmp_path)


def _cleanup_sqlite(conn: sqlite3.Connection | None, tmp_path: str | None) -> None:
    """Close ``conn`` and remove ``tmp_path`` if it was a temporary copy."""
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if tmp_path:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def parse_chrome(path: str) -> list[HistoryVisit]:
    """Parse a Chrome/Chromium ``History`` (or ``Archived History``) SQLite DB.

    Joins ``visits`` to ``urls`` to recover a ``(url, visit_time)`` pair per
    visit; if the ``visits`` table is missing/unusable, falls back to
    ``urls.last_visit_time``.  Chrome timestamps are microseconds since
    1601-01-01 UTC and are converted to POSIX seconds.

    Never raises; returns ``[]`` on failure.
    """
    visits: list[HistoryVisit] = []
    conn = None
    tmp_path = None
    try:
        conn, tmp_path = _open_sqlite_resilient(path)
        conn.text_factory = lambda b: b.decode("utf-8", "replace")

        rows = None
        try:
            rows = conn.execute(
                "SELECT urls.url, visits.visit_time "
                "FROM visits JOIN urls ON visits.url = urls.id"
            ).fetchall()
        except sqlite3.Error as exc:
            log.info(
                "history: chrome 'visits' table unusable in %s (%s); "
                "falling back to urls.last_visit_time",
                path,
                exc,
            )

        if rows is None:
            # Fallback: one visit per url using its last_visit_time.
            rows = conn.execute(
                "SELECT url, last_visit_time FROM urls"
            ).fetchall()

        for url, raw_ts in rows:
            video_id = _video_id_from_url(url)
            if not video_id:
                continue
            ts = _chrome_ts_to_posix(raw_ts)
            visits.append(HistoryVisit(video_id=video_id, timestamp=ts))
    except (sqlite3.Error, OSError) as exc:
        log.warning("history: failed to parse chrome history %s: %s", path, exc)
        return []
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.warning("history: unexpected error parsing chrome %s: %s", path, exc)
        return []
    finally:
        _cleanup_sqlite(conn, tmp_path)
    return visits


def _chrome_ts_to_posix(raw_ts: object) -> float:
    """Convert a Chrome microsecond-since-1601 timestamp to POSIX seconds."""
    try:
        value = float(raw_ts)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return value / 1e6 - _CHROME_EPOCH_OFFSET


def parse_firefox_places(path: str) -> list[HistoryVisit]:
    """Parse a Firefox ``places.sqlite`` history DB.

    Joins ``moz_places`` to ``moz_historyvisits``; ``visit_date`` is
    microseconds since the POSIX epoch.

    Never raises; returns ``[]`` on failure.
    """
    visits: list[HistoryVisit] = []
    conn = None
    tmp_path = None
    try:
        conn, tmp_path = _open_sqlite_resilient(path)
        conn.text_factory = lambda b: b.decode("utf-8", "replace")

        rows = None
        try:
            rows = conn.execute(
                "SELECT moz_places.url, moz_historyvisits.visit_date "
                "FROM moz_historyvisits "
                "JOIN moz_places ON moz_historyvisits.place_id = moz_places.id"
            ).fetchall()
        except sqlite3.Error as exc:
            log.info(
                "history: firefox 'moz_historyvisits' unusable in %s (%s); "
                "falling back to moz_places.last_visit_date",
                path,
                exc,
            )

        if rows is None:
            rows = conn.execute(
                "SELECT url, last_visit_date FROM moz_places"
            ).fetchall()

        for url, raw_ts in rows:
            video_id = _video_id_from_url(url)
            if not video_id:
                continue
            ts = _firefox_ts_to_posix(raw_ts)
            visits.append(HistoryVisit(video_id=video_id, timestamp=ts))
    except (sqlite3.Error, OSError) as exc:
        log.warning("history: failed to parse firefox places %s: %s", path, exc)
        return []
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.warning("history: unexpected error parsing firefox %s: %s", path, exc)
        return []
    finally:
        _cleanup_sqlite(conn, tmp_path)
    return visits


def _firefox_ts_to_posix(raw_ts: object) -> float:
    """Convert a Firefox microsecond-since-1970 timestamp to POSIX seconds."""
    try:
        value = float(raw_ts)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return value / 1e6


def parse_firefox_mork(path: str) -> list[HistoryVisit]:
    """Best-effort extractor for the legacy Firefox ``history.dat`` Mork DB.

    Mork is an ancient, hard-to-parse text format.  Rather than implement a full
    parser, this reads the file as latin-1 text and regexes out any YouTube
    watch URLs.  Reliably associating each URL with its last-visit timestamp
    column is not attempted, so every emitted :class:`HistoryVisit` has
    ``timestamp=0.0`` (the caller treats ``0`` as "unknown time").

    Never raises; returns ``[]`` on failure.
    """
    visits: list[HistoryVisit] = []
    try:
        with open(path, "r", encoding="latin-1", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        log.warning("history: cannot read mork file %s: %s", path, exc)
        return []

    try:
        seen: set[str] = set()
        for m in _MORK_WATCH_RE.finditer(text):
            video_id = m.group(1)
            # The Mork blob can repeat the same URL many times; de-dupe so we
            # do not flood the caller with identical zero-timestamp visits.
            if video_id in seen:
                continue
            seen.add(video_id)
            visits.append(HistoryVisit(video_id=video_id, timestamp=0.0))
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.warning("history: unexpected error parsing mork %s: %s", path, exc)
        return []
    return visits


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("usage: python -m lib.history <history-file>", file=sys.stderr)
        raise SystemExit(2)

    for visit in parse_history_file(sys.argv[1]):
        if visit.timestamp == 0:
            when = "unknown"
        else:
            when = datetime.utcfromtimestamp(visit.timestamp).isoformat()
        print("{} {}".format(visit.video_id, when))
