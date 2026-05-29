# Data directory

Decache's data files. All of these are tracked in the repository, so matching
works out of the box:

- `video_data.txt`      — the lost-media database: one record per line,
  `title|ids|phash|min-duration|max-duration` (durations as `HH:MM:SS.ss`).
- `watch_page_data.txt` — YouTube video ids whose watch pages identify assets.
- `unique_names.txt`    — IE unique-filename hints.
- `history_data.txt`    — history-URL match terms.
- `asset_data.txt`      — asset URL/domain search terms.

Anything else that lands here (runtime-generated output) is git-ignored.

See https://sindexmon.github.io/decache/ for more.
