"""Video verification: ffmpeg frame extraction, perceptual-hash matching, and
duration / browser-history correlation.

This reproduces the classification described in the project's About page:

* A candidate file's duration is measured and used to narrow the lost-media
  database down to records whose known duration range it falls inside.
* If any candidate has a known earliest-frame perceptual hash, the file's frames
  are hashed (via the native ``phash`` helper) and compared.  A Hamming match
  (distance <= 3, enforced inside ``phash``) marks the file **verified**.
* Otherwise the candidate ids are looked up in the user's browser history.  A
  visit within ~1.5 h of the file's mtime, when it's the *only* candidate, marks
  the file **likely** (promotable to verified later).  A visit within ~12.5 h,
  or multiple candidates, marks it **unverified** for manual review.
* No candidate at all -> the file is discarded (unless ``keep_all``).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional

from .database import Database, VideoRecord, clean_title

log = logging.getLogger("decache.verify")


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

# History-window thresholds (seconds), matching date_to_unix.vbs.
CONFIRM_WINDOW = 3600 * 1.5          # 1.5 h  -> rating 1
LIKELY_WINDOW = 86400 / 2 + 3600 / 2  # 12.5 h -> rating 2

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass
class MatchResult:
    status: str          # 'verified' | 'likely' | 'unverified' | 'discard'
    title: Optional[str] = None   # cleaned, human-readable (joined with " OR " if several)
    rarity: int = 3


class Verifier:
    def __init__(self, db: Database, history_index: Dict[str, List[float]],
                 ffmpeg: str = "ffmpeg", phash: str = "phash", workdir: str = "."):
        self.db = db
        self.history = history_index
        self.ffmpeg = ffmpeg
        self.phash = phash
        self.workdir = workdir

    # -- ffmpeg helpers ----------------------------------------------------
    def get_duration(self, path: str) -> Optional[float]:
        """Measured duration in seconds, or None if ffmpeg can't read it."""
        try:
            proc = subprocess.run([self.ffmpeg, "-nostdin", "-i", path],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("ffmpeg duration probe failed for %s: %s", path, exc)
            return None
        text = proc.stderr.decode("utf-8", "replace")
        m = _DURATION_RE.search(text)
        if not m:
            return None
        h, mm, ss = m.group(1), m.group(2), m.group(3)
        try:
            return int(h) * 3600 + int(mm) * 60 + float(ss)
        except ValueError:
            return None

    def extract_frame_hashes(self, path: str, target_hashes: List[str]) -> set:
        """Return the set of target hashes that matched a frame of the file.

        Mirrors :compareFrames -> ffmpeg scales every frame to 32x32 gray raw
        video, and the native ``phash`` helper reports which target hashes are
        within Hamming distance 3 of any frame.
        """
        if not target_hashes:
            return set()
        # A unique temp file per call so concurrent verifications don't clobber
        # each other's frames (this method runs in a thread pool).
        try:
            fd, raw_path = tempfile.mkstemp(suffix=".raw", dir=self.workdir)
            os.close(fd)
        except OSError:
            fd, raw_path = tempfile.mkstemp(suffix=".raw")
            os.close(fd)
        try:
            subprocess.run(
                [self.ffmpeg, "-nostdin", "-y", "-i", path, "-vf", "scale=32:32",
                 "-pix_fmt", "gray", "-f", "rawvideo", raw_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("ffmpeg frame extraction failed for %s: %s", path, exc)
            _safe_remove(raw_path)
            return set()
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            _safe_remove(raw_path)
            return set()
        try:
            proc = subprocess.run([self.phash, raw_path, *target_hashes],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("phash failed for %s: %s", path, exc)
            return set()
        finally:
            _safe_remove(raw_path)
        matched = set()
        for line in proc.stdout.decode("ascii", "replace").splitlines():
            tok = line.split()
            if tok:
                matched.add(tok[0].lower())
        return matched

    # -- history correlation ----------------------------------------------
    def _history_rating(self, record: VideoRecord, file_mtime: float) -> int:
        """Best (lowest) rating across all of a record's ids' visit times."""
        best = 3
        for vid in record.ids:
            for visit_ts in self.history.get(vid, ()):  # type: ignore[arg-type]
                if not visit_ts:
                    continue
                diff = abs(file_mtime - visit_ts)
                if diff < CONFIRM_WINDOW:
                    return 1
                if diff < LIKELY_WINDOW and best > 2:
                    best = 2
        return best

    # -- main entry point --------------------------------------------------
    def verify(self, path: str) -> MatchResult:
        """Classify a saved candidate video file."""
        duration = self.get_duration(path)
        if duration is None:
            return MatchResult("discard")

        candidates = self.db.records_in_duration(duration)
        if not candidates:
            return MatchResult("discard")

        # Phash pass: collect every candidate's known frame hash, ask phash.
        target_hashes = [c.phash for c in candidates if c.has_phash]
        matched = self.extract_frame_hashes(path, target_hashes) if target_hashes else set()
        if matched:
            for c in candidates:
                if c.has_phash and c.phash.lower() in matched:
                    return MatchResult("verified", clean_title(c.title), rarity=1)

        # History pass.
        try:
            file_mtime = os.path.getmtime(path)
        except OSError:
            file_mtime = 0.0

        titles: List[str] = []
        rarity = 3
        for c in candidates:
            rating = self._history_rating(c, file_mtime)
            if rating < 3:
                if not titles:
                    titles.append(clean_title(c.title))
                    if rating == 1:
                        rarity = 2
                else:
                    titles.append(clean_title(c.title))
                    rarity = 3

        if titles:
            joined = " OR ".join(titles)
            status = "likely" if rarity == 2 else "unverified"
            return MatchResult(status, joined, rarity=rarity)

        return MatchResult("discard")


def build_history_index(visits) -> Dict[str, List[float]]:
    """Group a flat list of HistoryVisit into {video_id: [timestamps]}."""
    index: Dict[str, List[float]] = {}
    for v in visits:
        index.setdefault(v.video_id, []).append(v.timestamp)
    return index
