# Decache

Decache is a specialized web-cache scanner for pieces of lost media, with a
focus on deleted YouTube videos. It scans a computer (or a backup/disk image of
one) for browser caches, recovers cached video files, and compares them against
a database of lost media — sorting confident matches into `Verified` and
possible matches into `Unverified` for manual review.

This is the **Linux / Unix port**: pure `bash` + `python3`, with no Wine and no
Windows tooling. (It also runs on macOS.)

## Requirements

- **Python 3.7+** (standard library only)
- **ffmpeg** — for reading video durations and extracting frames
  (`apt install ffmpeg`, `dnf install ffmpeg`, `brew install ffmpeg`, …)
- **A C++ compiler** (`g++` or `clang++`) — to build the small `phash`
  perceptual-hashing helper. Built automatically on first run.

No browser-specific tools are needed: the cache and history formats (Chromium,
Firefox, Internet Explorer, and old Opera) are parsed directly in Python, so the
NirSoft utilities the Windows version shelled out to are gone.

## Setup

```sh
./build.sh          # compiles bin/phash from phash/phash.cpp (also auto-runs on first launch)
```

The lost-media database and its auxiliary inputs ship with this repository under
`bin/data/` (`video_data.txt`, `watch_page_data.txt`, `unique_names.txt`,
`history_data.txt`, `asset_data.txt`) — matching works out of the box. See
`bin/data/README.md` for what each file is.

## Usage

```sh
./decache "/mnt/backups/Old Laptop"      # scan a specific path
./decache                                # prompt for a path interactively
./decache computers.txt                  # scan every path listed in a text file (one per line)
```

Options:

| Option | Meaning |
| --- | --- |
| `--keep-all` | Save all candidate videos to `Unverified`, even unmatched ones. |
| `--silence 1` | Ignore all errors; make no interactive prompts. |
| `--silence 2` | Like `1`, but still attempt one ownership/permission recovery. |
| `-j` / `--jobs N` | Parallel workers for cache parsing and ffmpeg/phash verification (default: number of CPUs; `1` = serial). |
| `--dump-urls FILE` | Write every cache URL the parsers extracted to `FILE` (debugging). |
| `-v` / `-vv` | Verbose / debug logging. |

The legacy Windows-style switches `/keepall` and `/silence:N` are still accepted
for compatibility.

Verified findings are written to `Verified/` and archived into `Assets.zip`.
Unverified candidates go to `Unverified/` — open them in VLC or any media player
to review.

## How it works

1. **Discover** — walk the target tree and identify browser cache directories
   (Chromium `Cache`/`Cache_Data`, Firefox `cache2`, IE `Content.IE5`,
   Presto-Opera) and history databases (Chrome `History`, Firefox
   `places.sqlite` / legacy `history.dat`). Unreadable directories are skipped.
2. **Recover** — parse each cache directly to pull out response bodies and their
   original URLs (decompressing gzip/deflate), reassembling chunked/fragmented
   WebM and MP4 streams.
3. **Index-driven recovery** (port of `read_cache.bat`) — any cached file whose
   URL matches the asset database is trusted and copied straight into `Verified`
   as `[N]filename`, with its (ip-redacted) URL logged to `Verified/contents.txt`
   and its origin to `bin/private_locations.txt`:
   - URLs matching an `asset_data.txt` domain/term;
   - YouTube `videoplayback` URLs whose `o-…` asset id was learned by reading the
     cached watch pages for the ids in `watch_page_data.txt`;
   - files whose name matches a `unique_names.txt` glob (e.g. `*.swf`).
4. **Loose-file scan** (port of the original's Temp globbing) — stray media that
   isn't inside a recognised cache is also picked up: `fla*.tmp` / `get_video*` /
   `videoplayback*` and `unique_names.txt` globs anywhere, plus broad
   `*.flv`/`*.mp4`/`*.webm`/`*.on2` only inside temp-like directories (so a user's
   media library isn't swept up). These files are **copied**, never moved.
5. **Verify the rest** — for video files *not* matched by the index, identify the
   container by magic bytes (FLV / MP4 / WebM), then:
   - measure the duration with ffmpeg and narrow the database to records with a
     matching duration range;
   - compare frames against each candidate's known perceptual hash
     (`bin/phash`, Hamming distance ≤ 3) → **verified**;
   - otherwise correlate the video id against the user's browser history: a
     visit within ~1.5 h of the file's timestamp (and unique) → **verified**;
     within ~12.5 h → **unverified** for manual review.

   Cache parsing, history parsing, and this verification step run across
   `--jobs` workers (default: all CPUs).
6. **Package** — zip the verified findings into `Assets.zip`.

## Project layout

```
decache            bash launcher
decache.py         main orchestrator (replaces start_decache.bat)
build.sh           compiles the phash helper (replaces build.bat)
phash/phash.cpp    perceptual-hash helper (cross-platform)
lib/               the Python implementation
  cache_common.py    shared dataclasses
  config.py          paths + external-tool discovery
  database.py        loads bin/data/*.txt (video/asset/watch-page/unique data)
  scanner.py         cache/history discovery
  chromium_cache.py  Chromium simple + block-file cache parser
  firefox_cache.py   Firefox cache2 (+ legacy) parser
  ie_cache.py        Internet Explorer index.dat parser
  opera_cache.py     Presto-Opera cache parser (best-effort)
  history.py         Chrome/Firefox history parsers
  magic.py           video header detection
  recover.py         index-driven recovery (port of read_cache.bat)
  reassemble.py      chunk reassembly + verify of unindexed videos
  verify.py          ffmpeg + phash + duration/history matching
  packaging.py       filenames, mtime preservation, zipping
  ui.py              CLI prompts (replace the old VBScript dialogs)
tests/             integration, recovery, and loose-scan smoke tests
```

## Migrating from the Windows version

The old `.bat` orchestration, `.vbs` helper/dialog scripts, the bundled
`ffmpeg.exe` / `dd.exe` / `7z` / `phash.exe`, and the NirSoft `*CacheView` /
`*HistoryView` executables have all been removed and reimplemented in Python or
replaced by native Linux tools (system `ffmpeg`, Python's `zipfile`). The
`bin/data/` database format is unchanged, so existing data files work as-is.
