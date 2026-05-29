#!/usr/bin/env python3
"""End-to-end smoke test for the Decache Linux port.

Generates a real MP4 with ffmpeg, wraps it in a synthetic Chromium simple-cache
entry under a YouTube URL, seeds the lost-media database and browser history,
then runs the full pipeline and asserts the video is recovered and verified via
the history-window heuristic.

Run:  python3 tests/integration_test.py
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SIMPLE_FILE_MAGIC = 0xFCFB6D1BA7725C30
SIMPLE_EOF_MAGIC = 0xF4FA6F45970D41D8


def make_simple_cache_entry(url: bytes, body: bytes) -> bytes:
    """Build a minimal valid Chromium simple-cache <hash>_0 file."""
    header = struct.pack("<QIII", SIMPLE_FILE_MAGIC, 5, len(url), 0)
    # stream-0 EOF: magic, flags, crc, stream_size
    eof = struct.pack("<QIIQ", SIMPLE_EOF_MAGIC, 0, 0, len(body))
    return header + url + body + eof


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not installed")
        return 0

    work = tempfile.mkdtemp(prefix="decache_test_")
    target = os.path.join(work, "drive")
    cache_dir = os.path.join(target, "Cache_Data")
    os.makedirs(cache_dir)

    # 1. Generate a real ~4-second MP4.
    video = os.path.join(work, "sample.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=64x64:rate=10",
         "-pix_fmt", "yuv420p", video],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    with open(video, "rb") as fh:
        body = fh.read()

    # 2. Wrap it in a synthetic Chromium simple-cache entry.
    vid_id = "abcdefghijk"
    url = f"https://www.youtube.com/watch?v={vid_id}".encode()
    entry = make_simple_cache_entry(url, body)
    entry_path = os.path.join(cache_dir, "0123456789abcdef_0")
    with open(entry_path, "wb") as fh:
        fh.write(entry)
    # Set its mtime to "now" so the seeded history visit is within the window.
    now = time.time()
    os.utime(entry_path, (now, now))

    # 3. Seed the lost-media database (duration ~4s, no phash -> history path).
    data_dir = os.path.join(ROOT, "bin", "data")
    os.makedirs(data_dir, exist_ok=True)
    video_data = os.path.join(data_dir, "video_data.txt")
    backup = None
    if os.path.exists(video_data):
        backup = video_data + ".bak"
        os.replace(video_data, backup)
    with open(video_data, "w") as fh:
        # title | ids | phash | dur_min | dur_max
        fh.write(f'"Test+Lost+Video|{vid_id}|0000000000000000|3.0|5.0"\n')

    # 4. Seed Firefox history (places.sqlite) with a visit near "now".
    import sqlite3
    places = os.path.join(target, "places.sqlite")
    con = sqlite3.connect(places)
    con.executescript(
        "CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, last_visit_date INTEGER);"
        "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER);")
    visit_us = int(now * 1_000_000)
    con.execute("INSERT INTO moz_places VALUES (1, ?, ?)",
                (f"https://www.youtube.com/watch?v={vid_id}", visit_us))
    con.execute("INSERT INTO moz_historyvisits VALUES (1, 1, ?)", (visit_us,))
    con.commit()
    con.close()

    # Clean any prior results.
    for d in ("Verified", "Unverified"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p)

    # 5. Run the pipeline (non-interactive: stdin not a TTY here).
    rc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "decache.py"), target, "-v"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = rc.stdout.decode()
    print(out)

    # 6. Assertions.
    verified = os.listdir(os.path.join(ROOT, "Verified")) if os.path.isdir(os.path.join(ROOT, "Verified")) else []
    unverified = os.listdir(os.path.join(ROOT, "Unverified")) if os.path.isdir(os.path.join(ROOT, "Unverified")) else []
    print("Verified:", verified)
    print("Unverified:", unverified)

    ok = True
    media_verified = [f for f in verified if f.endswith((".mp4", ".webm", ".flv"))]
    if not media_verified:
        print("FAIL: expected a verified media file (history-window promotion)")
        ok = False
    else:
        print("PASS: recovered + verified", media_verified)
    if not os.path.exists(os.path.join(ROOT, "Assets.zip")):
        print("FAIL: Assets.zip not created")
        ok = False
    else:
        print("PASS: Assets.zip created")

    # Cleanup repo artifacts and restore DB.
    os.remove(video_data)
    if backup:
        os.replace(backup, video_data)
    for d in ("Verified", "Unverified"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    if os.path.exists(os.path.join(ROOT, "Assets.zip")):
        os.remove(os.path.join(ROOT, "Assets.zip"))
    shutil.rmtree(work, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
