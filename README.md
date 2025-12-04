# mpv music wrapper

CLI music player (mpv wrapper) that plays directly from your library with optional on-the-fly per-track ReplayGain (no pre-scan or duplicate ReplayGain library), smart cover-art handling (scans folder/subfolders + embedded, picks best by keyword/resolution), RAM staging (keeps your disks/library untouched), and mpv IPC control (pause/skip/status from other tools). Supports random library shuffle, whole-album playback, and playlist playback from common playlist formats.

<img src="2025-12-03_13-12.jpg" alt="Screenshot" width="60%" />

## Features
- Modes: random (`--random-mode=full-library --library=...`), album (`--album=DIR`), playlist (`--playlist=FILE`).
- Optional normalization (`--normalize`): copy audio to tmpfs, strip existing ReplayGain tags, add track ReplayGain via metaflac (FLAC only), and start mpv with `--replaygain=track`. This is done on the fly—no advance library scan or maintaining a second ReplayGain’d library. Without it: still copies/strips ReplayGain tags when possible, but no ReplayGain scan and mpv uses `--replaygain=no`.
- RAM staging: per-track subdirs under `/dev/shm/mpv-music-<pid>-XXXXXX`; cleaned as playback advances and on exit. Library is never modified and no disk writes are done during playback.
- Cover art: scans every image in the track folder and all subfolders, extracts embedded art to PNG, selects the best image (keywords > resolution > size > name), converts to `cover.png`, strips embedded art from the temp audio copy, and exposes only `cover.png` to mpv (`--cover-art-auto=exact`).
- IPC/GUI: mpv runs with a GUI window forced open; IPC socket at `/tmp/mpv-<pid>.sock` so you can pause/skip/query status from other terminals/scripts. Poll interval for playlist position is 5s.
- Logging: per-track ReplayGain line and ART candidates, with a separator line after each track. Startup header is auto-sized with mode, path, socket, track count, buffer, and normalize status. Optional `ART_DEBUG=1` for verbose art selection logs.

## Requirements
- bash, find, shuf
- mpv
- python (for IPC helper)
- ffprobe and ffmpeg
- metaflac (required when using `--normalize`; otherwise ReplayGain strip/scan is skipped for FLAC)

## Usage
Run from the repository root (or place the script on PATH):

```bash
# Random mode (library required)
./mpv_music_wrapper.sh --random-mode=full-library --library=/home/johndoe/music/ --normalize

# Album mode
./mpv_music_wrapper.sh --album=/path/to/album --normalize
# Album mode with library (enables parent cover search for multi-disc albums)
./mpv_music_wrapper.sh --album=/home/johndoe/music/Album --library=/home/johndoe/music/ --normalize

# Playlist mode
./mpv_music_wrapper.sh --playlist=/path/to/list.m3u8 --normalize
```

You can omit `--normalize` to skip ReplayGain scanning (files are still copied/stripped; mpv uses `--replaygain=no`).

For album mode, adding `--library=/path/to/your/music` lets the wrapper fall back to album-level cover art (folder directly under the library root) when tracks live in disc subfolders.

### Examples for shell functions / aliases
Add to `~/.bashrc` (adjust paths as needed):
```bash
play_random_music(){ ./mpv_music_wrapper.sh --random-mode=full-library --library=/home/johndoe/music/ --normalize; }
play_album(){ ./mpv_music_wrapper.sh --album="$1" --library=/home/johndoe/music/ --normalize; }
```

Then use:
```bash
play_random_music
play_album /path/to/album
```

## Cover selection details
- Preferred keywords: `cover`, `front`, `folder` (beats resolution/area). You can adjust the keyword list in `PREFERRED_IMAGE_KEYWORDS` in the script to match your own naming.
- When multiple keyword matches: higher resolution wins; tie -> earlier keyword; tie -> larger file size; tie -> filename.
- When no keywords: higher resolution; tie -> size; tie -> filename.
- Embedded art is extracted to a temp PNG and participates in selection; if it loses, it is removed. The chosen image is converted to `cover.png` in the track’s temp dir. Embedded art is stripped from the temp audio copy so mpv only sees external `cover.png`.
- Multi-disc albums: when `--library` is provided (random mode always has it; album mode if you pass it) and the album lives inside that library, the script also searches for art in the album folder directly under the library root (in addition to the disc folder). Keyworded images in the current disc folder still win over parent-folder images.

## Playlists
- Supported: m3u/m3u8/pls/cue. Non-audio entries are skipped with warnings. Relative paths are resolved against the playlist location.

## Behavior and safety
- Library is read-only; all processing occurs on temp copies in `/dev/shm/mpv-music-<pid>-XXXXXX`.
- Per-track temps are cleaned as playback advances; everything is cleaned on exit.
- IPC socket: `/tmp/mpv-<pid>.sock`.
- Poll interval: 5 seconds.

## Controlling mpv via IPC helper
`mpv-send-key.sh` sends simple controls to any mpv IPC socket (default glob `/tmp/mpv-*`, filters to real sockets):

```bash
./mpv-send-key.sh pause      # toggle pause
./mpv-send-key.sh next       # playlist next (weak)
./mpv-send-key.sh prev       # playlist prev (weak)

# Target a specific socket (or glob)
./mpv-send-key.sh pause '/tmp/mpv-music-*'

# Debug (shows sockets found/errors)
MPV_SEND_DEBUG=1 ./mpv-send-key.sh pause
```

You can bind these commands to global hotkeys in your desktop environment/window manager (e.g., map Pause/Play/PgUp/PgDn keys to run `./mpv-send-key.sh pause|next|prev`).

## Debugging
- Set `ART_DEBUG=1` to print detailed art candidate selection, stored cover meta, and track separators.
