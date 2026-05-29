#!/usr/bin/env python3
"""Decache — a web-cache scanner for pieces of lost media (Linux port).

This is the pure-Python replacement for ``start_decache.bat``.  It scans a
target directory (a mounted backup, a copied user profile, or a whole drive
image) for browser caches, recovers cached video files, and classifies them
against a lost-media database into ``Verified`` / ``Unverified`` folders, then
zips the verified findings.

Usage
-----
    decache.py [PATH] [--keep-all] [--silence {1,2}] [-v]

PATH may be:
  * a directory to scan, or
  * a text file with one directory path per line, or
  * omitted, in which case you are prompted for a path.

Legacy Windows-style switches ``/keepall`` and ``/silence:N`` are also accepted.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from lib import scanner
from lib.config import Paths, Tools
from lib.database import Database, load_database
from lib import history as history_mod
from lib import packaging
from lib import ui
from lib.reassemble import LIKELY_MARK, SEP_MARK, VIDEO_EXTS as _VIDEO_EXTS
from lib.recover import Recoverer
from lib.verify import Verifier, build_history_index

log = logging.getLogger("decache")


def scan_target(target: str, db: Database, paths: Paths, tools: Tools,
                keep_all: bool, recoverer: "Recoverer", jobs: int = 1) -> Recoverer:
    """Scan one target: discover caches, build history, run index-driven recovery.

    A single ``Recoverer`` is shared across every scanned target so the asset
    numbering (``[N]``), ``contents.txt`` manifest, and cached-id set stay
    continuous for the whole run.  ``jobs`` parallelises cache parsing and the
    (ffmpeg/phash) verification pass.
    """
    log.info("scanning %s", target)
    # Loose-file globs: specific patterns are matched anywhere; broad video
    # extensions only inside temp-like dirs (so a media library isn't swept up).
    loose_anywhere = ["fla*.tmp", "get_video*", "videoplayback*"] + list(db.unique_globs)
    loose_temp = ["*.flv", "*.on2", "*.webm", "*.mp4"]
    locations, history_files, loose_files = scanner.discover(target, loose_anywhere, loose_temp)

    # Build the history index (video id -> visit timestamps), parsing the DBs
    # in parallel (each parse is independent and mostly I/O).
    if jobs > 1 and len(history_files) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            visit_lists = list(ex.map(history_mod.parse_history_file, history_files))
    else:
        visit_lists = [history_mod.parse_history_file(hf) for hf in history_files]
    visits = [v for lst in visit_lists for v in lst]
    history_index = build_history_index(visits)
    log.info("history: %d visits across %d ids", len(visits), len(history_index))
    recoverer.verifier.history = history_index

    # Parse cache locations in parallel (decompression releases the GIL), then
    # feed them sequentially so reassembly ordering / shared counters stay safe.
    if jobs > 1 and len(locations) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            parsed = list(ex.map(lambda loc: (loc, list(scanner.parse_location(loc))), locations))
    else:
        parsed = [(loc, list(scanner.parse_location(loc))) for loc in locations]
    for loc, entries in parsed:
        recoverer.process_location(entries, label=f"[{loc.family}] {loc.path}")

    # Loose files outside any recognised cache (e.g. fla*.tmp in Temp).
    for lf in loose_files:
        recoverer.process_loose_file(lf)

    recoverer.flush(jobs)
    return recoverer


def promote_likely(paths: Paths) -> None:
    """Promote unique 'likely' (@1@) videos to Verified (post-pass)."""
    try:
        unv = [f for f in os.listdir(paths.unverified)
               if os.path.isfile(os.path.join(paths.unverified, f))]
    except OSError:
        return
    verified_titles = set()
    try:
        for f in os.listdir(paths.verified):
            verified_titles.add(f.split(" @")[0])
    except OSError:
        pass

    # Group by title (text before the first " @").
    groups: Dict[str, List[str]] = {}
    for f in unv:
        title = f.split(" @")[0]
        groups.setdefault(title, []).append(f)

    for title, group in groups.items():
        likely = [f for f in group if LIKELY_MARK in f]
        if likely and len(group) == 1 and title not in verified_titles:
            src = likely[0]
            dst_name = packaging.free_name(paths.verified, src.replace(LIKELY_MARK, SEP_MARK))
            src_path = os.path.join(paths.unverified, src)
            dst_path = os.path.join(paths.verified, dst_name)
            try:
                st = os.stat(src_path)
                os.replace(src_path, dst_path)
                os.utime(dst_path, (st.st_atime, st.st_mtime))
                log.info("promoted likely -> verified: %s", title)
            except OSError as exc:
                log.debug("promotion failed for %s: %s", src, exc)

    # Normalise any remaining @1@ markers to plain separators.
    for f in os.listdir(paths.unverified):
        if LIKELY_MARK in f:
            src = os.path.join(paths.unverified, f)
            dst = os.path.join(paths.unverified, packaging.free_name(
                paths.unverified, f.replace(LIKELY_MARK, SEP_MARK)))
            try:
                st = os.stat(src)
                os.replace(src, dst)
                os.utime(dst, (st.st_atime, st.st_mtime))
            except OSError:
                pass


def write_credit_and_finish(paths: Paths, cached_ids: set) -> None:
    """Prompt for claim info, write credit.txt, optionally share ids, then zip."""
    def _count(folder: str) -> int:
        try:
            return sum(1 for f in os.listdir(folder)
                       if os.path.isfile(os.path.join(folder, f)))
        except OSError:
            return 0

    # verified files excluding bookkeeping files
    verified_files = _count(paths.verified)
    for bookkeeping in ("contents.txt", "credit.txt", "cached_ids.txt"):
        if os.path.exists(os.path.join(paths.verified, bookkeeping)):
            verified_files -= 1
    verified_files = max(verified_files, 0)
    num_videos = len(cached_ids)

    claim = ui.ask_for_name(verified_files, num_videos)

    credit_path = os.path.join(paths.verified, "credit.txt")
    if claim.identifier:
        with open(credit_path, "w", encoding="utf-8") as fh:
            fh.write(f"PRIVATE:{claim.identifier}\n")
            fh.write(f"PUBLIC:{claim.public_cred}\n")
        # Watermark each unverified file with the identifier (faithful to the
        # original; trailing bytes are ignored by media players).
        for f in os.listdir(paths.unverified):
            fp = os.path.join(paths.unverified, f)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
                with open(fp, "a", encoding="utf-8", errors="ignore") as wf:
                    wf.write(f"\n{claim.identifier}\n{claim.public_cred}\n")
                os.utime(fp, (st.st_atime, st.st_mtime))
            except OSError:
                pass
    else:
        if os.path.exists(credit_path):
            os.remove(credit_path)

    if claim.send_ids and cached_ids:
        with open(os.path.join(paths.verified, "cached_ids.txt"), "a", encoding="utf-8") as fh:
            for cid in sorted(cached_ids):
                fh.write(cid + "\n")

    archive = packaging.zip_folder(paths.verified, paths.root, "Assets.zip")
    if archive:
        ui.notice(f"Verified findings archived to: {os.path.basename(archive)}")
        if claim.identifier:
            ui.notice("You may upload it directly through the Decache website:\n"
                      "  https://sindexmon.github.io/decache/")


def _resolve_one(path_arg: str) -> List[str]:
    """Resolve a single argument: a directory, or a file listing directories."""
    if os.path.isdir(path_arg):
        return [path_arg]
    if os.path.isfile(path_arg):
        targets = []
        with open(path_arg, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if os.path.exists(line):
                    targets.append(line)
                else:
                    log.warning("listed path does not exist: %s", line)
        return targets
    log.error("path does not exist: %s", path_arg)
    return []


def resolve_targets(path_args: List[str]) -> List[str]:
    """Resolve every argument; each may be a directory or a list-file.

    Multiple folders can be passed and are all scanned. Order is preserved and
    duplicates are removed.
    """
    if not path_args:
        chosen = ui.prompt_folder(
            "Select a computer/backup to scan. The running computer is usually "
            "mounted at '/'; a backup's location varies.")
        return [chosen] if chosen else []
    targets: List[str] = []
    seen = set()
    for arg in path_args:
        for t in _resolve_one(arg):
            key = os.path.abspath(t)
            if key not in seen:
                seen.add(key)
                targets.append(t)
    return targets


def normalize_legacy_args(argv: List[str]) -> List[str]:
    """Translate Windows-style /keepall and /silence:N switches."""
    out = []
    for a in argv:
        low = a.lower()
        if low == "/keepall":
            out.append("--keep-all")
        elif low.startswith("/silence"):
            # /silence:1 or /silence:2
            level = a.split(":", 1)[1] if ":" in a else "1"
            out.extend(["--silence", level])
        else:
            out.append(a)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = normalize_legacy_args(argv)

    parser = argparse.ArgumentParser(
        prog="decache", description="Scan a backup for cached lost media.")
    parser.add_argument("path", nargs="*", help="one or more directories to "
                        "scan, and/or text files listing directories (one per "
                        "line); all are processed")
    parser.add_argument("--keep-all", action="store_true",
                        help="keep all candidate videos even if unmatched")
    parser.add_argument("--silence", type=int, choices=(1, 2), default=0,
                        help="suppress interactive error prompts")
    parser.add_argument("--dump-urls", metavar="FILE",
                        help="write every cache URL seen to FILE (for debugging "
                        "what the parsers actually extracted)")
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help="parallel workers for cache parsing and ffmpeg/phash "
                        "verification (default: number of CPUs; 1 = serial)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    root = os.path.dirname(os.path.abspath(__file__))
    paths = Paths.discover(root)
    os.makedirs(paths.bin_dir, exist_ok=True)
    os.makedirs(paths.data_dir, exist_ok=True)
    paths.ensure_dirs()
    tools = Tools.discover(paths.bin_dir)

    import shutil
    if not shutil.which(tools.ffmpeg) and not os.path.exists(tools.ffmpeg):
        log.warning("ffmpeg not found; duration/frame checks will be skipped "
                    "and nothing will verify. Install ffmpeg.")
    if not os.path.exists(tools.phash):
        log.warning("phash helper not found at %s; run ./build.sh to compile it. "
                    "Perceptual-hash matching will be skipped.", tools.phash)

    db = load_database(paths.data_dir)

    targets = resolve_targets(args.path)
    if not targets:
        print("No target selected; nothing to do.", file=sys.stderr)
        return 1

    verifier = Verifier(db, {}, ffmpeg=tools.ffmpeg, phash=tools.phash,
                        workdir=paths.work_dir)
    recoverer = Recoverer(db, verifier, paths.verified, paths.unverified,
                          os.path.join(paths.bin_dir, "private_locations.txt"),
                          args.keep_all)
    if args.dump_urls:
        recoverer.dump_fh = open(args.dump_urls, "w", encoding="utf-8", errors="replace")
    jobs = args.jobs if args.jobs and args.jobs > 0 else (os.cpu_count() or 1)
    log.info("using %d parallel worker(s)", jobs)
    try:
        for target in targets:
            scan_target(target, db, paths, tools, args.keep_all, recoverer, jobs)
    finally:
        if recoverer.dump_fh is not None:
            recoverer.dump_fh.close()
            print(f"Wrote all cache URLs seen to {args.dump_urls}", file=sys.stderr)
    cached_ids = recoverer.cached_ids

    # Diagnostic summary: distinguishes "found nothing" from "parsed nothing".
    t = recoverer.totals
    log.info("scan totals: %d cache entries parsed, %d video candidate(s), "
             "%d asset(s) recovered, %d distinct video id(s) in cache",
             t["entries"], t["video_candidates"], t["assets"], len(recoverer.seen_video_ids))
    if recoverer.seen_video_ids:
        log.info("video ids seen in cache URLs: %s",
                 ", ".join(sorted(recoverer.seen_video_ids)))

    promote_likely(paths)

    def _media_count(folder: str) -> int:
        try:
            return sum(1 for f in os.listdir(folder)
                       if os.path.isfile(os.path.join(folder, f))
                       and f.lower().endswith(_VIDEO_EXTS))
        except OSError:
            return 0

    n_verified = _media_count(paths.verified)
    n_unverified = _media_count(paths.unverified)

    write_credit_and_finish(paths, cached_ids)

    ui.notice("Thank you for using Decache!\n"
              "For more information see: https://sindexmon.github.io/decache/")
    print(f"Results: {n_verified} verified, {n_unverified} unverified "
          f"(see the Unverified folder; open with VLC or any media player).")
    print(f"Scanned {t['entries']} cache entries; {t['video_candidates']} looked "
          f"like video, {t['assets']} matched the asset database.")
    if t["entries"] == 0:
        print("No cache entries were parsed — the cache parsers found nothing "
              "readable here. Re-run with -vv, or use --dump-urls FILE to inspect.",
              file=sys.stderr)
    elif n_verified == 0 and n_unverified == 0 and t["assets"] == 0:
        print("Cache entries were parsed but none matched the lost-media "
              "database. Use --dump-urls FILE to see every URL that was scanned.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
