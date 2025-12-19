# mpv music wrapper

CLI music player (mpv wrapper) that plays directly from your library with optional on-the-fly per-track ReplayGain (no pre-scan or duplicate ReplayGain library), smart cover-art handling (scans folder/subfolders + embedded, picks best by keyword/resolution), RAM staging (keeps your disks/library untouched), and mpv IPC control (pause/skip/status from other tools). Supports random library shuffle, whole-album playback, and playlist playback from common playlist formats. **The wrapper is now implemented in Python** for portability and fewer dependencies (originally Bash). Repo: [github.com/julienmn/mpv-music-wrapper](https://github.com/julienmn/mpv-music-wrapper)

<img src="2025-12-03_13-12.jpg" alt="Screenshot" width="60%" />

## Features
- Modes: random (`--random-mode=full-library --library=...`), album (`--album=DIR`), playlist (`--playlist=FILE`).
- Optional normalization (`--normalize`): copy audio to tmpfs, strip existing ReplayGain tags, add track ReplayGain via ffmpeg loudnorm (all formats), and start mpv with `--replaygain=track`. This is done on the fly—no advance library scan or maintaining a second ReplayGain’d library. Without it: still copies/strips ReplayGain tags when possible, but no ReplayGain scan and mpv uses `--replaygain=no`.
- RAM staging: per-track subdirs under `/dev/shm/mpv-music-<pid>-XXXXXX`; cleaned as playback advances and on exit. Library is never modified and no disk writes are done during playback.
- Cover art: scans every image in the track folder and all subfolders, extracts embedded art to PNG, selects the best image (front-ish > album-named > everything else, then scope/size), converts to `cover.png`, strips embedded art from the temp audio copy, and exposes only `cover.png` to mpv (`--cover-art-auto=exact`). Album-name matching ignores junk tokens (pure numbers, very short tokens, audio extensions like `flac`/`mp3`).
- IPC/GUI: mpv runs with a GUI window forced open; IPC socket at `/tmp/mpv-<pid>.sock` so you can pause/skip/query status from other terminals/scripts. Poll interval for playlist position is 5s.
- Logging: per-track ReplayGain line and ART candidates, with a separator line after each track. Startup header is auto-sized with mode, path, socket, track count, buffer, and normalize status. Optional `ART_DEBUG=1` for verbose art selection logs.

## Requirements
- python 3
- mpv
- ffmpeg (ffprobe included)

## Usage
Run from the repository root (or place the script on PATH):

```bash
# Random mode (library required)
./mpv_music_wrapper.py --random-mode=full-library --library=/home/johndoe/music/ --normalize

# Album mode
./mpv_music_wrapper.py --album=/path/to/album --normalize
# Album mode with library (enables parent cover search for multi-disc albums)
./mpv_music_wrapper.py --album=/home/johndoe/music/Album --library=/home/johndoe/music/ --normalize

# Playlist mode
./mpv_music_wrapper.py --playlist=/path/to/list.m3u8 --normalize
```

You can omit `--normalize` to skip ReplayGain scanning (files are still copied/stripped; mpv uses `--replaygain=no`).

Pass additional mpv flags via `--mpv-additional-args="--your --mpv-flags"`.

For album mode, adding `--library=/path/to/your/music` lets the wrapper fall back to album-level cover art (folder directly under the library root) when tracks live in disc subfolders.

### Examples for shell functions / aliases
Add to `~/.bashrc` (adjust paths as needed):
```bash
play_random_music(){ ./mpv_music_wrapper.py --random-mode=full-library --library=/home/johndoe/music/ --normalize; }
play_album(){ ./mpv_music_wrapper.py --album="$1" --library=/home/johndoe/music/ --normalize; }
```

Then use:
```bash
play_random_music
play_album /path/to/album
```

## Cover selection details
- Where it searches:
  - Track folder and subfolders, plus embedded art (extracted to PNG).
  - With `--library` in album/random mode and multi-disc layouts: also the album’s top folder (other discs and non-disc folders).
- Tokens and keywords:
  - Preferred keywords: `cover`, `front`, `folder` (tunable via constants if needed).
  - Non-front words: `back`, `tray`, `cd`, `disc`, `inlay`, `inlet`, `booklet`, `book`, `spine`, `rear`, `inside`, `tracklisting`.
  - Album-name tokens: normalized album folder name (lowercase, punctuation stripped, drops pure numbers/short tokens/audio extensions). Image basenames normalized the same way.
- Buckets:
  - Bucket 1 (front): shape is squarish or portrait, no non-front words (unless also in album name), and has a front keyword or album-name overlap ≥ 0.75.
  - Bucket 2: everything else.
- Selection:
  - If Bucket 1 exists: largest area wins; ties go to better scope (embedded > track-folder > album-root > other-disc), then keyword rank (cover > front > folder), then trailing integer.
  - If Bucket 1 is only tiny images but Bucket 2 has non-tiny, take the best non-tiny in Bucket 2 instead.
- If Bucket 1 is empty: choose from Bucket 2. Prefer squarish images if any; when squarish areas are similar, ignore area and tie-break by scope → name tokens → keyword → trailing number; otherwise area still leads (scope first).
- Embedded art: treated like any other candidate; if it loses, it is removed. The winner is linked as `cover.png` and embedded art is stripped from the staged audio copy so mpv only sees `cover.png`.

For more detail, see [`cover_selection_spec.md`](cover_selection_spec.md).

## Random algorithm
- Current random mode is `full-library`. Libraries with <50 albums shuffle all tracks once (uniform, no replacement).
- Libraries with ≥50 albums use album-spread: build an album → tracks map, pick a random album not seen in the recent albums window, then a random track from that album. The recent-albums window is ~10% of album count, clamped to 20–200 and never reaching the full album count.
- Recently played albums are avoided until they age out of the recent-albums window; playback continues indefinitely with albums rotating back in after they fall out of the window.
- The library is fully rescanned every hour and the album/track pool is rebuilt (recent albums list is kept, entries for deleted albums are dropped). Newly added albums can start playing without restarting the script.
- Tunables live near the top of `mpv_music_wrapper.py` (e.g., album thresholds, recent-albums percent/min/max, rescan interval).
- Optional: `--persist-recent-albums` saves/loads the recent albums list to a JSON cache in your user cache dir so frequent restarts don’t repeat the same albums. You can override the cache path via the `RECENT_ALBUMS_CACHE_PATH_OVERRIDE` constant (leave it `None` to use the platform default).
## Playlists
- Supported: m3u/m3u8/pls/cue. Non-audio entries are skipped with warnings. Relative paths are resolved against the playlist location.

## Behavior and safety
- Library is read-only; all processing occurs on temp copies in RAM (`/dev/shm/mpv-music-<pid>-XXXXXX` when available; override with `MPV_MUSIC_TMPDIR`).
- Per-track temps are cleaned as playback advances; everything is cleaned on exit.
- IPC socket: `/tmp/mpv-<pid>.sock`.
- Poll interval: 5 seconds.

## Controlling mpv via IPC helper
`mpv_send_key.py` sends simple controls to any mpv IPC socket (default glob `/tmp/mpv-*`, filters to real sockets):

```bash
./mpv_send_key.py pause      # toggle pause
./mpv_send_key.py next       # playlist next (weak)
./mpv_send_key.py prev       # playlist prev (weak)

# Target a specific socket (or glob)
./mpv_send_key.py pause '/tmp/mpv-music-*'

# Debug (shows sockets found/errors)
MPV_SEND_DEBUG=1 ./mpv_send_key.py pause
```

You can bind these commands to global hotkeys in your desktop environment/window manager (e.g., map Pause/Play/PgUp/PgDn keys to run `./mpv_send_key.py pause|next|prev`).

## Debugging
- Set `ART_DEBUG=1` to print detailed art candidate selection, stored cover meta, and track separators.

## Development and tests
See [tests/DEV.md](tests/DEV.md) for dev/test setup (pytest, helper scripts). Runtime usage doesn’t require any of that.
