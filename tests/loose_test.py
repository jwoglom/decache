#!/usr/bin/env python3
"""Test loose-file scanning (files outside any recognised cache).

Mirrors the original's Temp-folder globbing: a unique-name asset anywhere, and a
broad video extension only inside a temp-like directory. Confirms the backup is
never modified (files are copied).
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    work = tempfile.mkdtemp(prefix="decache_loose_")
    target = os.path.join(work, "drive")
    # A unique-name asset (matches a unique_names.txt glob) in a normal folder.
    os.makedirs(os.path.join(target, "Documents"))
    asset = os.path.join(target, "Documents", "mainpage_final_game.swf")
    with open(asset, "wb") as fh:
        fh.write(b"CWS\x0f" + b"\x00" * 100)
    # A broad-extension file in a NON-temp dir: must be IGNORED (no sweep of
    # the user's media library).
    movie = os.path.join(target, "Documents", "home_movie.mp4")
    with open(movie, "wb") as fh:
        fh.write(b"\x00" * 5000)
    # A fla*.tmp in a Temp dir: must be PICKED UP (anywhere pattern).
    os.makedirs(os.path.join(target, "AppData", "Local", "Temp"))
    fla = os.path.join(target, "AppData", "Local", "Temp", "fla1A2B.tmp")
    with open(fla, "wb") as fh:
        fh.write(b"\x00" * 100)  # not real video -> will be discarded, but discovered

    for d in ("Verified", "Unverified"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)

    out = subprocess.run([sys.executable, os.path.join(ROOT, "decache.py"), target,
                          "-v", "--keep-all"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()

    ok = True
    # The unique-name asset must be recovered as [N]...swf.
    verified = os.listdir(os.path.join(ROOT, "Verified")) if os.path.isdir(os.path.join(ROOT, "Verified")) else []
    if any(f.endswith(".swf") and f.startswith("[") for f in verified):
        print("PASS: loose unique-name asset recovered:", [f for f in verified if f.endswith(".swf")])
    else:
        print("FAIL: loose unique-name asset NOT recovered; Verified:", verified)
        print(out)
        ok = False

    # The loose .mp4 in a non-temp dir must NOT have been swept up. With
    # --keep-all, anything processed lands in Unverified; the home movie should
    # not appear there.
    unverified = os.listdir(os.path.join(ROOT, "Unverified")) if os.path.isdir(os.path.join(ROOT, "Unverified")) else []
    if any("home_movie" in f for f in unverified):
        print("FAIL: non-temp .mp4 was swept up (false positive):", unverified)
        ok = False
    else:
        print("PASS: non-temp media library .mp4 correctly ignored")

    # The fla*.tmp must have been *discovered* (mentioned in the verbose log).
    if "fla1A2B.tmp" in out:
        print("PASS: fla*.tmp in Temp discovered")
    else:
        print("FAIL: fla*.tmp in Temp not discovered")
        ok = False

    # Backup must be untouched.
    if os.path.exists(asset) and os.path.exists(movie) and os.path.exists(fla):
        print("PASS: backup files left intact (copied, not moved)")
    else:
        print("FAIL: a backup file was modified/removed")
        ok = False

    for d in ("Verified", "Unverified"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    for f in ("Assets.zip",):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            os.remove(p)
    pl = os.path.join(ROOT, "bin", "private_locations.txt")
    if os.path.exists(pl):
        os.remove(pl)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
