#!/usr/bin/env python3
"""
mpv music wrapper (Python rewrite)

Preserves behavior of the original Bash version with staging in tmpfs/ramdisk,
cover art selection, optional normalization, and mpv IPC control.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

# -----------------
# Constants / config
# -----------------

AUDIO_EXTS = ["flac", "mp3", "ogg", "opus", "m4a", "alac", "wav", "aiff", "wv"]
PLAYLIST_EXTS = ["m3u", "m3u8", "pls", "cue"]
IMAGE_EXTS = ["jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff", "tif", "svg"]
PREFERRED_IMAGE_KEYWORDS = ["cover", "front", "folder"]
NON_FRONT_IMAGE_KEYWORDS = [
    "back",
    "tray",
    "cd",
    "disc",
    "inlay",
    "inlet",
    "booklet",
    "book",
    "spine",
    "rear",
    "inside",
    "tracklisting",
]
TINY_FRONT_AREA = 200_000
IMAGE_PROBE_BIN = "ffprobe"
IMAGE_EXTRACT_BIN = "ffmpeg"
COVER_LNORM_BIN = "ffmpeg"
COVER_PREFERRED_FILE = "cover.png"
AREA_THRESHOLD_PCT = 75
ASPECT_MIN_AREA_BUCKET1 = 3 * TINY_FRONT_AREA  # require decent size before preferring squarer in bucket 1
ASPECT_MIN_AREA_OTHER = TINY_FRONT_AREA  # lower floor for non-front buckets
ASPECT_AREA_RATIO_MIN = 0.5  # smaller image must be at least this fraction of larger area to let aspect decide
ALBUM_SPREAD_THRESHOLD = 50
RECENT_ALBUMS_MIN = 20
RECENT_ALBUMS_MAX = 200
RECENT_ALBUMS_PCT = 10
RECENT_ALBUMS_CACHE_PATH_OVERRIDE: Optional[str] = None  # Set to override platform default cache path; leave None for auto
RANDOM_RESCAN_INTERVAL = 3600
BUFFER_AHEAD = 1
POLL_INTERVAL = 5
TMPDIR_ENV = "MPV_MUSIC_TMPDIR"
DEFAULT_SOCKET_DIR = Path("/tmp")
WINDOWS_PIPE_PREFIX = r"\\.\\pipe\\"

ART_DEBUG = os.environ.get("ART_DEBUG", "0") == "1"
LOUDNORM_AVAILABLE = False

# --------------
# Logging helpers
# --------------

def log_info(msg: str) -> None:
    print(f"[info] {msg}", file=sys.stderr)


def log_warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    log_error(msg)
    sys.exit(code)


# -------------
# Data classes
# -------------

@dataclasses.dataclass
class CoverCandidate:
    path: Path
    width: int
    height: int
    area: int
    size_bytes: int
    pref_kw_count: int
    name_token_score: int
    has_non_front: bool
    bucket: int
    kw_rank: int
    scope_rank: int
    scope: str
    src_type: str  # embedded|external
    name: str
    album_tokens: List[str]
    rel_display: str
    is_embedded: bool = False


@dataclasses.dataclass
class TrackInfo:
    index: int
    source_path: Path
    staged_path: Path
    cover_path: Optional[Path]
    cover_meta: str
    cover_detail: str


@dataclasses.dataclass
class RandomPlanner:
    library: Path
    albums: List[Path]
    album_track_files: Dict[Path, List[Path]]
    album_track_count: Dict[Path, int]
    total_track_count: int
    album_spread_mode: bool
    recent_albums_size: int
    recent_albums: deque[Path]
    tracks: List[Path]
    last_rescan: float

    @classmethod
    def from_library(cls, library: Path) -> "RandomPlanner":
        albums, album_track_files, album_track_count, total_track_count = build_album_map(library)
        album_spread_mode = len(albums) >= ALBUM_SPREAD_THRESHOLD
        recent_albums_size = compute_recent_albums_size(len(albums)) if album_spread_mode else 0
        recent_albums: deque[Path] = deque(maxlen=recent_albums_size)
        tracks = gather_random_tracks(library, album_spread_mode, albums, album_track_files)
        return cls(
            library=library,
            albums=albums,
            album_track_files=album_track_files,
            album_track_count=album_track_count,
            total_track_count=total_track_count,
            album_spread_mode=album_spread_mode,
            recent_albums_size=recent_albums_size,
            recent_albums=recent_albums,
            tracks=tracks,
            last_rescan=time.time(),
        )

    def maybe_refresh_album_map(self) -> bool:
        now = time.time()
        if now - self.last_rescan < RANDOM_RESCAN_INTERVAL:
            return False
        old_albums = list(self.albums)
        old_set = set(old_albums)
        old_track_count = self.total_track_count

        self.albums, self.album_track_files, self.album_track_count, self.total_track_count = build_album_map(self.library)
        self.recent_albums = deque([h for h in self.recent_albums if h in self.album_track_count], maxlen=self.recent_albums.maxlen)

        added = sum(1 for a in self.albums if a not in old_set)
        removed = sum(1 for a in old_albums if a not in set(self.album_track_count.keys()))
        delta = self.total_track_count - old_track_count
        if added or removed or delta != 0:
            # mpv status lines can stay on the same terminal line; emit a blank line for readability.
            print(file=sys.stderr)
            log_info(
                f"random rescan: albums={len(self.albums)} (added {added}, removed {removed}) "
                f"tracks={self.total_track_count} (delta {delta})"
            )
        self.last_rescan = now
        return True

    def choose_track_in_album(self, album: Path) -> Optional[Path]:
        lst = self.album_track_files.get(album, [])
        if not lst:
            return None
        return random.choice(lst)


# ------------------------
# Album spread helpers
# ------------------------

def compute_recent_albums_size(total_albums: int) -> int:
    """
    Compute the recent albums window size using the same rules as the main loop.
    """
    size = max(RECENT_ALBUMS_MIN, min(RECENT_ALBUMS_MAX, max(1, total_albums * RECENT_ALBUMS_PCT // 100)))
    if size >= total_albums:
        size = max(0, total_albums - 1)
    return size


# -------------------------------
# Recent albums persistence (opt-in)
# -------------------------------

def default_recent_albums_cache_path() -> Path:
    system = platform.system().lower()
    if system == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif system == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "mpv-music-wrapper" / "recent_albums.json"


def resolve_recent_albums_cache_path() -> Path:
    if RECENT_ALBUMS_CACHE_PATH_OVERRIDE:
        return Path(RECENT_ALBUMS_CACHE_PATH_OVERRIDE).expanduser()
    return default_recent_albums_cache_path()


def load_recent_albums_cache(path: Path, planner: RandomPlanner) -> None:
    if not path.is_file():
        log_info(f"recent albums persistence: cache not found at {path} (will create on exit)")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_warn(f"recent albums persistence: failed to read {path}: {exc}")
        return

    loaded = [Path(p) for p in data if isinstance(p, str)]
    found = len(loaded)
    existing = [p for p in loaded if p in planner.album_track_count]
    kept_existing = len(existing)
    trimmed = existing[-planner.recent_albums_size :] if planner.recent_albums_size > 0 else []
    planner.recent_albums.extend(trimmed)

    dropped_missing = found - kept_existing
    trimmed_off = kept_existing - len(trimmed)
    log_info(
        f"recent albums persistence: load path={path} found={found} kept={len(trimmed)} "
        f"dropped_missing={dropped_missing} trimmed_to_window={trimmed_off}"
    )


def save_recent_albums_cache(path: Path, albums: Sequence[Path]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump([str(p) for p in albums], fh, indent=2)
        # mpv status lines can stick to stderr; add a newline for readability.
        print(file=sys.stderr)
        log_info(f"recent albums persistence: saved {len(albums)} entries to {path}")
    except OSError as exc:
        log_warn(f"recent albums persistence: failed to write {path}: {exc}")


def choose_album_for_play(albums_list: List[Path], recent_albums: List[Path], recent_size: int) -> Optional[Path]:
    """
    Pick a random album, avoiding the last `recent_size` entries when possible.
    """
    if not albums_list:
        return None
    blocked = set(recent_albums[-recent_size:]) if recent_size > 0 else set()
    candidates = [a for a in albums_list if a not in blocked]
    if not candidates:
        candidates = list(albums_list)
    return random.choice(candidates)


# ----------------
# Utility functions
# ----------------

def lower_ext(path: Path) -> str:
    return path.suffix[1:].lower() if path.suffix else ""


def ext_in_list(ext: str, items: Sequence[str]) -> bool:
    return ext.lower() in items


def is_audio(path: Path) -> bool:
    return ext_in_list(lower_ext(path), AUDIO_EXTS)


def is_image(path: Path) -> bool:
    return ext_in_list(lower_ext(path), IMAGE_EXTS)


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1B\[[0-9;]*[mK]", "", s)


def visible_len(s: str) -> int:
    s = strip_ansi(s)
    for emoji in ("🎵", "🔀", "💾", "🔁", "🎲", "💿", "🎯", "📜"):
        s = s.replace(emoji, "aa")
    return len(s)


def normalize_name_tokens(name: str) -> List[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"[^0-9A-Za-z]+", " ", s)
    toks = [t.lower() for t in s.split() if t]
    return toks


def clean_album_tokens(name: str) -> List[str]:
    toks = normalize_name_tokens(name)
    cleaned: List[str] = []
    for t in toks:
        if not t:
            continue
        if t.isdigit():
            continue
        if len(t) <= 2:
            continue
        if t in AUDIO_EXTS:
            continue
        cleaned.append(t)
    return cleaned


def token_overlap_score(base: List[str], target: List[str]) -> int:
    target_set = set(target)
    return sum(1 for t in base if t and t in target_set)


def extract_trailing_int(name: str) -> Optional[int]:
    m = re.search(r"(\d+)(?!.*\d)", name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def human_rescan_interval(seconds: int) -> str:
    if seconds <= 0:
        return "off"
    minutes = seconds // 60
    hours = minutes // 60
    minutes %= 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def display_path(p: Path, display_root: Path) -> str:
    if not p:
        return ""
    try:
        p_abs = p.resolve()
    except OSError:
        p_abs = p
    try:
        root_abs = display_root.resolve()
    except OSError:
        root_abs = display_root
    if p_abs == root_abs:
        return "."
    try:
        rel = p_abs.relative_to(root_abs)
        return str(rel)
    except ValueError:
        return str(p)


# -----------------
# Argument parsing
# -----------------

def usage_text() -> str:
    return (
        "Usage:\n"
        "  mpv_music_wrapper.sh --random-mode=full-library --library /path/to/lib [--normalize] [--mpv-additional-args='...']\n"
        "  mpv_music_wrapper.sh --album /path/to/album [--normalize] [--mpv-additional-args='...']\n"
        "  mpv_music_wrapper.sh --playlist /path/to/list.m3u [--normalize] [--mpv-additional-args='...']\n\n"
        "Modes (choose one):\n"
        "  --random-mode=full-library   Shuffle any audio file under --library recursively.\n"
        "  --album <dir>                Play audio files under <dir> (sorted, non-random).\n"
        "  --playlist <file>            Play entries from <file> (sorted as given).\n\n"
        "Options:\n"
        "  --library <dir>              Required for --random-mode. Optional for --album to enable multi-disc\n"
        "                               parent cover search when the album is inside the library.\n"
        "  --normalize                  Copy to RAM, strip existing RG tags, add track RG via ffmpeg\n"
        "                               loudnorm, and play with --replaygain=track (with clip protection).\n"
        "                               Without this flag we still copy to RAM, strip RG tags when\n"
        "                               possible, and link album art, but do NOT compute RG or pass\n"
        "                               --replaygain to mpv.\n"
        "  --mpv-additional-args <str>  Extra args for mpv (string, split like a shell).\n"
        "  --persist-recent-albums      Save/load recent album picks between runs (JSON cache, optional).\n"
        "  -h, --help                   Show this help.\n\n"
        "Examples:\n"
        "  mpv_music_wrapper.sh --random-mode=full-library --library /music --normalize\n"
        "  mpv_music_wrapper.sh --album /music/Artist/Album\n"
        "  mpv_music_wrapper.sh --playlist ~/lists/favorites.m3u8 --normalize\n"
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--random-mode")
    parser.add_argument("--album")
    parser.add_argument("--playlist")
    parser.add_argument("--library")
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--mpv-additional-args")
    parser.add_argument("--persist-recent-albums", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")

    known, unknown = parser.parse_known_args(argv)
    if known.help:
        print(usage_text())
        sys.exit(0)
    if unknown:
        die(f"Unsupported argument(s): {' '.join(unknown)}")

    mode = None
    random_mode = None
    album_dir = None
    playlist_file = None
    library = None

    if known.random_mode:
        if known.random_mode != "full-library":
            die(f"Unsupported random mode: {known.random_mode}")
        mode = "random"
        random_mode = known.random_mode
    if known.album:
        if mode:
            die("One mode is required: --random-mode=full-library, --album, or --playlist")
        mode = "album"
        album_dir = known.album
    if known.playlist:
        if mode:
            die("One mode is required: --random-mode=full-library, --album, or --playlist")
        mode = "playlist"
        playlist_file = known.playlist
    if not mode:
        die("One mode is required: --random-mode=full-library, --album, or --playlist")

    if known.library:
        library = known.library

    # Validation mirroring Bash
    if mode == "random":
        if not library:
            die("--library is required for --random-mode=full-library")
        if not Path(library).is_dir():
            die(f"Library path not found: {library}")
    elif mode == "album":
        if not album_dir:
            die("--album requires a directory path")
        if not Path(album_dir).is_dir():
            die(f"Album directory not found: {album_dir}")
        if library and not Path(library).is_dir():
            die(f"Library path not found: {library}")
    elif mode == "playlist":
        if not playlist_file:
            die("--playlist requires a file path")
        if not Path(playlist_file).is_file():
            die(f"Playlist file not found: {playlist_file}")
        ext = lower_ext(Path(playlist_file))
        if ext not in PLAYLIST_EXTS:
            die(f"Unsupported playlist extension: {ext}")

    mpv_additional_args: List[str] = []
    if known.mpv_additional_args:
        try:
            mpv_additional_args = shlex.split(known.mpv_additional_args)
        except ValueError as e:
            die(f"Failed to parse --mpv-additional-args: {e}")

    return argparse.Namespace(
        mode=mode,
        random_mode=random_mode,
        album_dir=album_dir,
        playlist_file=playlist_file,
        library=library,
        normalize=known.normalize,
        mpv_additional_args=mpv_additional_args,
        persist_recent_albums=known.persist_recent_albums,
    )


# -------------------
# Temp & dependency setup
# -------------------

def choose_tmp_root() -> Path:
    override = os.environ.get(TMPDIR_ENV)
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="mpv-music-", dir=str(p)))

    system = platform.system().lower()
    if system == "linux":
        shm = Path("/dev/shm")
        if shm.exists() and shm.is_dir():
            try:
                return Path(tempfile.mkdtemp(prefix="mpv-music-", dir=str(shm)))
            except Exception:
                log_warn("Could not create tmpdir under /dev/shm; falling back to system temp")
        log_warn("/dev/shm missing or unusable; staging may not be in RAM. Set MPV_MUSIC_TMPDIR to a tmpfs/ramdisk for best performance.")
    elif system == "darwin":
        log_warn("Staging under system temp (likely disk). Set MPV_MUSIC_TMPDIR to a ramdisk for RAM staging.")
    else:
        log_warn("Staging under system temp. Set MPV_MUSIC_TMPDIR to a ramdisk for RAM staging.")

    try:
        return Path(tempfile.mkdtemp(prefix="mpv-music-"))
    except Exception as e:
        die(f"Could not create temporary directory: {e}")


def check_dependencies(normalize: bool) -> None:
    required = ["mpv", IMAGE_PROBE_BIN, IMAGE_EXTRACT_BIN, "python", COVER_LNORM_BIN]
    for dep in required:
        if shutil.which(dep) is None:
            die(f"{dep} not found in PATH")
    globals()["LOUDNORM_AVAILABLE"] = shutil.which(COVER_LNORM_BIN) is not None
    if normalize and not LOUDNORM_AVAILABLE:
        die("--normalize requested but ffmpeg (loudnorm) not found")


# --------------
# Filesystem ops
# --------------

def find_images_recursive(dir_path: Path) -> List[Path]:
    results: List[Path] = []
    for root, _, files in os.walk(dir_path):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            if is_image(p):
                results.append(p)
    return results


def album_root_for_track(track: Path, library: Optional[Path]) -> Optional[Path]:
    if not library or not library.is_dir():
        return None
    try:
        track_abs = track.resolve()
        lib_abs = library.resolve()
    except OSError:
        return None
    if not str(track_abs).startswith(str(lib_abs) + os.sep):
        return None
    rel = track_abs.relative_to(lib_abs)
    parts = rel.parts
    if not parts:
        return None
    candidate = lib_abs / parts[0]
    if candidate.is_dir():
        return candidate
    return None


# --------------
# Cover handling
# --------------

def image_dims_area(path: Path) -> Tuple[int, int, int]:
    try:
        proc = subprocess.run(
            [IMAGE_PROBE_BIN, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        line = proc.stdout.splitlines()[0] if proc.stdout else ""
        if not line:
            return 0, 0, 0
        w_str, h_str = line.split("x")
        w, h = int(w_str), int(h_str)
        return w, h, w * h
    except Exception:
        return 0, 0, 0


def extract_embedded_cover(track: Path, dst_dir: Path) -> Optional[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = dst_dir / "embedded-cover.png"
    cmd = [IMAGE_EXTRACT_BIN, "-loglevel", "error", "-y", "-i", str(track), "-map", "0:v:0", "-frames:v", "1", str(out)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return out
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass
    return None


def gather_image_candidates(dir_path: Path, album_root: Optional[Path], is_multi: bool, audio_src: Path, extract_dir: Path) -> Tuple[List[Path], Optional[Path]]:
    seen: set[Path] = set()
    out: List[Path] = []
    for p in find_images_recursive(dir_path):
        if p not in seen:
            seen.add(p)
            out.append(p)
    if is_multi and album_root:
        for p in find_images_recursive(album_root):
            if p not in seen:
                seen.add(p)
                out.append(p)
    embedded = extract_embedded_cover(audio_src, extract_dir)
    if embedded:
        out.append(embedded)
    return out, embedded


def analyze_candidates(
    candidates_paths: List[Path],
    embedded_path: Optional[Path],
    track: Path,
    album_root: Optional[Path],
    dir_path: Path,
    base_root: Path,
    display_root: Path,
) -> Tuple[List[CoverCandidate], List[str]]:
    if album_root and album_root == dir_path.parent:
        album_token = dir_path.name
    else:
        album_token = album_root.name if album_root else dir_path.name
    album_tokens = clean_album_tokens(album_token)

    candidates_meta: List[CoverCandidate] = []
    detail_lines: List[str] = []

    for f in candidates_paths:
        w, h, area = image_dims_area(f)
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        lower = f.name.lower()
        base_noext = lower.rsplit(".", 1)[0]
        base_tokens = normalize_name_tokens(base_noext)
        pref_kw_count = 0
        kw_rank = 999
        name_token_score = 0
        has_non_front = False
        bucket = 3

        idx = 0
        for kw in PREFERRED_IMAGE_KEYWORDS:
            if kw in lower:
                pref_kw_count += 1
                if kw_rank == 999:
                    kw_rank = idx
            idx += 1

        for nf in NON_FRONT_IMAGE_KEYWORDS:
            if nf in base_tokens:
                has_non_front = True
                break

        if pref_kw_count == 0 and album_tokens:
            name_token_score = token_overlap_score(base_tokens, album_tokens)
        if pref_kw_count > 0 or (name_token_score > 0 and not has_non_front):
            bucket = 1
        elif name_token_score > 0:
            bucket = 2
        else:
            bucket = 3

        scope = "external"
        scope_rank = 2
        src_type = "external"
        disp_path = display_path(f, display_root)
        is_embedded = False
        if embedded_path and str(f) == str(embedded_path):
            scope = "embedded"
            scope_rank = 0
            src_type = "embedded"
            disp_path = f"(embedded from {display_path(track, display_root)})"
            is_embedded = True
        else:
            if album_root and str(f).startswith(str(album_root)):
                scope = "album-root"
                scope_rank = 1
            if str(f).startswith(str(dir_path)):
                scope = "disc"
                scope_rank = 0

        rel_path = disp_path
        if src_type == "external" and base_root and str(f).startswith(str(base_root)):
            rel_path = str(f.relative_to(base_root))
        elif src_type == "embedded":
            rel_path = "EMBEDDED"

        area_mp = area / 1_000_000
        size_mb = size / 1_000_000
        detail_lines.append(
            f"path={rel_path} res={w}x{h} area={area_mp:.1f}MP size={size_mb:.1f}MB "
            f"kwpref={pref_kw_count} nametoks={name_token_score} bucket={bucket} score={pref_kw_count + name_token_score}"
        )

        cand = CoverCandidate(
            path=f,
            width=w,
            height=h,
            area=area,
            size_bytes=size,
            pref_kw_count=pref_kw_count,
            name_token_score=name_token_score,
            has_non_front=has_non_front,
            bucket=bucket,
            kw_rank=kw_rank,
            scope_rank=scope_rank,
            scope=scope,
            src_type=src_type,
            name=f.name,
            album_tokens=album_tokens,
            rel_display=rel_path,
            is_embedded=is_embedded,
        )
        candidates_meta.append(cand)

    return candidates_meta, detail_lines


def select_best_cover(
    candidates: List[CoverCandidate],
    detail_lines: List[str],
    track: Path,
    display_root: Path,
    base_root: Optional[Path],
) -> Tuple[Optional[CoverCandidate], str, str]:
    best: Optional[CoverCandidate] = None

    def aspect_penalty(c: CoverCandidate) -> float:
        if c.width <= 0 or c.height <= 0:
            return float("inf")
        return abs(math.log(c.width / c.height))

    def aspect_can_override(c1: CoverCandidate, c2: CoverCandidate) -> bool:
        floor = ASPECT_MIN_AREA_BUCKET1 if c1.bucket == 1 else ASPECT_MIN_AREA_OTHER
        if c1.area < floor or c2.area < floor:
            return False
        area_ratio = min(c1.area, c2.area) / max(c1.area, c2.area)
        if area_ratio < ASPECT_AREA_RATIO_MIN:
            return False
        if c1.pref_kw_count != c2.pref_kw_count:
            return False
        if c1.has_non_front != c2.has_non_front:
            return False
        return True

    for cand in candidates:
        if best is None:
            best = cand
            continue

        pick = False
        allow_worse_scope = False
        if cand.scope_rank > best.scope_rank:
            if best.area > 0:
                if best.area * 100 < cand.area * AREA_THRESHOLD_PCT:
                    allow_worse_scope = True
            else:
                allow_worse_scope = True

        if cand.bucket < best.bucket:
            pick = True
        elif cand.bucket == best.bucket == 1:
            if cand.pref_kw_count > best.pref_kw_count:
                # Allow high-res album-named art (no keywords) to beat a smaller keyworded image.
                if best.pref_kw_count == 0 and best.name_token_score > 0 and not best.has_non_front:
                    if cand.area * 100 < best.area * AREA_THRESHOLD_PCT:
                        pick = False
                    else:
                        pick = True
                else:
                    pick = True
            elif cand.pref_kw_count == best.pref_kw_count:
                if cand.pref_kw_count == 0 and not cand.has_non_front and cand.name_token_score > best.name_token_score and cand.area * 100 >= best.area * AREA_THRESHOLD_PCT:
                    pick = True
                elif cand.pref_kw_count == 0 and not cand.has_non_front and cand.name_token_score == best.name_token_score and cand.area * 100 >= best.area * AREA_THRESHOLD_PCT and cand.scope_rank <= best.scope_rank:
                    pick = True
                elif cand.pref_kw_count > 0 and best.pref_kw_count > 0:
                    if not cand.has_non_front and best.has_non_front and cand.area >= TINY_FRONT_AREA:
                        pick = True
                    elif cand.has_non_front and not best.has_non_front and best.area < TINY_FRONT_AREA and cand.area * 100 >= best.area * (100 + (100 - AREA_THRESHOLD_PCT)):
                        pick = True
                    elif cand.scope_rank < best.scope_rank and cand.area * 100 >= best.area * AREA_THRESHOLD_PCT:
                        pick = True
                    elif cand.scope_rank == best.scope_rank and cand.area > best.area:
                        pick = True
                    elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank < best.kw_rank:
                        pick = True
                elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank == best.kw_rank and cand.size_bytes > best.size_bytes:
                    pick = True
                elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank == best.kw_rank and cand.size_bytes == best.size_bytes:
                    cand_num = extract_trailing_int(cand.name) or float("inf")
                    best_num = extract_trailing_int(best.name) or float("inf")
                    if cand_num != best_num:
                        pick = cand_num < best_num
                    else:
                        pick = cand.name < best.name
                elif cand.scope_rank > best.scope_rank and allow_worse_scope and cand.area > best.area:
                    pick = True
                elif cand.pref_kw_count == 0 and not cand.has_non_front and best.pref_kw_count > 0 and cand.name_token_score > 0 and cand.area * 100 >= best.area * AREA_THRESHOLD_PCT:
                    pick = True
        elif cand.bucket == best.bucket == 2:
            if cand.name_token_score > best.name_token_score:
                pick = True
            elif cand.name_token_score == best.name_token_score:
                within = best.area == 0 or cand.area * 100 >= best.area * AREA_THRESHOLD_PCT
                if cand.scope_rank < best.scope_rank and within:
                    pick = True
                elif cand.scope_rank == best.scope_rank and cand.area > best.area:
                    pick = True
                elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.size_bytes > best.size_bytes:
                    pick = True
                elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.size_bytes == best.size_bytes and cand.name < best.name:
                    pick = True
                elif cand.scope_rank > best.scope_rank and allow_worse_scope and cand.area > best.area:
                    pick = True
        elif cand.bucket == best.bucket == 3:
            if best.area >= ASPECT_MIN_AREA_OTHER and cand.area >= ASPECT_MIN_AREA_OTHER and aspect_penalty(best) <= aspect_penalty(cand):
                # Keep squarer art even if smaller when both are reasonably sized.
                continue
            if cand.scope_rank < best.scope_rank:
                pick = True
            elif cand.scope_rank == best.scope_rank and cand.area > best.area:
                if not (aspect_can_override(best, cand) and aspect_penalty(best) <= aspect_penalty(cand)):
                    pick = True
            elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank < best.kw_rank:
                pick = True
            elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank == best.kw_rank and cand.size_bytes > best.size_bytes:
                pick = True
            elif cand.scope_rank == best.scope_rank and cand.area == best.area and cand.kw_rank == best.kw_rank and cand.size_bytes == best.size_bytes and cand.name < best.name:
                pick = True
            elif cand.scope_rank > best.scope_rank and allow_worse_scope and cand.area > best.area:
                pick = True

        if not pick and cand.bucket == best.bucket and cand.scope_rank == best.scope_rank:
            if aspect_can_override(cand, best):
                if aspect_penalty(cand) <= aspect_penalty(best):
                    pick = True

        if pick:
            best = cand

    cover_meta = ""
    cover_detail = "[ ] no images found"

    if best:
        cover_meta = f"{best.src_type}|{best.width}|{best.height}|{best.area}|{best.pref_kw_count + best.name_token_score}|{best.size_bytes}"
        formatted: List[str] = []
        for line in detail_lines:
            if (best.src_type == "embedded" and "(embedded" in line) or line.startswith(f"path={best.rel_display}"):
                formatted.append(f"[*] {line}")
            else:
                formatted.append(f"[ ] {line}")
        cover_detail = "\n".join(formatted) if formatted else "[ ] no images found"

    return best, cover_meta, cover_detail


def select_cover_for_track(track: Path, dst_dir: Path, audio_copy: Path, album_root: Optional[Path], display_root: Path) -> Tuple[Optional[Path], str, str]:
    dir_path = track.parent
    is_multi = False
    if album_root and dir_path != album_root and str(dir_path).startswith(str(album_root)):
        is_multi = True

    base_root = album_root if album_root else dir_path
    candidates_paths, embedded_path = gather_image_candidates(dir_path, album_root, is_multi, track, dst_dir)
    candidates_meta, detail_lines = analyze_candidates(
        candidates_paths,
        embedded_path,
        track,
        album_root,
        dir_path,
        base_root,
        display_root,
    )

    best, cover_meta, cover_detail = select_best_cover(candidates_meta, detail_lines, track, display_root, base_root)

    if embedded_path and best and embedded_path != best.path and embedded_path.exists():
        try:
            embedded_path.unlink()
        except OSError:
            pass

    if ART_DEBUG:
        print(f"[ARTDBG] track: {display_path(track, display_root)}", file=sys.stderr)
        print(f"[ARTDBG] candidates ({len(detail_lines)}):", file=sys.stderr)
        if detail_lines:
            for dl in detail_lines:
                print(f"  {dl}", file=sys.stderr)
        else:
            print("  (none)", file=sys.stderr)
        print(f"[ARTDBG] chosen: {best.src_type if best else 'none'} {display_path(best.path if best else Path(''), display_root)}", file=sys.stderr)

    cover_path = best.path if best else None
    return cover_path, cover_meta, cover_detail


def link_cover(cover: Path, dst_dir: Path) -> None:
    if not cover:
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / COVER_PREFERRED_FILE
    if cover != target:
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        try:
            target.symlink_to(cover)
        except OSError:
            shutil.copy2(cover, target)
    # remove other symlinks
    for item in dst_dir.iterdir():
        if item.is_symlink() and item.name != COVER_PREFERRED_FILE:
            try:
                item.unlink()
            except OSError:
                pass


def make_cover_png(src: Path, dst_dir: Path) -> Optional[Path]:
    if not src:
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / COVER_PREFERRED_FILE
    if src.suffix.lower() == ".png":
        if src != dst:
            try:
                if dst.exists():
                    dst.unlink()
                dst.symlink_to(src)
                return dst
            except OSError:
                pass
        try:
            shutil.copy2(src, dst)
            return dst
        except OSError:
            return None
    cmd = [IMAGE_EXTRACT_BIN, "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1", str(dst)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    return None


# --------------------
# Tag and audio handling
# --------------------

def strip_embedded_art(file: Path) -> None:
    if not file.exists():
        return
    ext = lower_ext(file)
    tmp = file.with_name(f"{file.stem}.noart{file.suffix}")
    cmd = [IMAGE_EXTRACT_BIN, "-loglevel", "error", "-nostdin", "-y", "-i", str(file), "-map", "0:a", "-map_metadata", "0", "-vn", "-dn", "-sn", "-c", "copy", str(tmp)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        try:
            tmp.replace(file)
            return
        except OSError:
            pass
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

def strip_id3_if_flac(file: Path) -> None:
    if lower_ext(file) != "flac":
        return
    # Remove ID3 via ffmpeg copy
    tmp = file.with_name(f"{file.stem}.clean{file.suffix}")
    cmd = [IMAGE_EXTRACT_BIN, "-loglevel", "error", "-nostdin", "-y", "-i", str(file), "-map", "0:a", "-map_metadata", "0", "-vn", "-dn", "-sn", "-c", "copy", str(tmp)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        try:
            tmp.replace(file)
            return
        except OSError:
            pass
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass


def strip_rg_tags_if_possible(file: Path) -> None:
    # Clear ReplayGain tags while preserving other metadata via ffmpeg copy.
    tmp = file.with_name(f"{file.stem}.norg{file.suffix}")
    cmd = [
        COVER_LNORM_BIN,
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(file),
        "-map",
        "0:a",
        "-map_metadata",
        "0",
        "-vn",
        "-dn",
        "-sn",
        "-c",
        "copy",
        "-metadata",
        "replaygain_track_gain=",
        "-metadata",
        "replaygain_track_peak=",
        "-metadata",
        "replaygain_reference_loudness=",
        str(tmp),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        try:
            tmp.replace(file)
            return
        except OSError:
            pass
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass


def add_replaygain_if_requested(file: Path, normalize: bool) -> None:
    if not normalize:
        return
    # Use ffmpeg loudnorm to measure and then tag with RG-equivalent tags.
    # Pass 1: measure loudness/peak
    measure_cmd = [
        COVER_LNORM_BIN,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(file),
        "-af",
        "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(measure_cmd, capture_output=True, text=True)
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not combined:
        log_warn(f"replaygain scan (loudnorm) failed for {file}; RG tags not added")
        return
    try:
        m = re.search(r"\{.*\}", combined, re.S)
        if not m:
            log_warn(f"replaygain scan parse failed for {file}: no loudnorm JSON found")
            return
        data = json.loads(m.group(0))
        measured_i = float(data.get("input_i", 0.0))
        max_true_peak = float(data.get("input_tp", 0.0))
    except Exception as exc:
        log_warn(f"replaygain scan parse failed for {file}: {exc}")
        return
    gain_db = -18.0 - measured_i
    peak_linear = 10 ** (max_true_peak / 20.0)
    tagged = file.with_name(f"{file.stem}.rg{file.suffix}")
    tag_cmd = [
        COVER_LNORM_BIN,
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(file),
        "-map",
        "0:a",
        "-map_metadata",
        "0",
        "-vn",
        "-dn",
        "-sn",
        "-c",
        "copy",
        "-metadata",
        f"replaygain_track_gain={gain_db:.2f} dB",
        "-metadata",
        f"replaygain_track_peak={peak_linear:.6f}",
        str(tagged),
    ]
    proc2 = subprocess.run(tag_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc2.returncode == 0 and tagged.exists() and tagged.stat().st_size > 0:
        try:
            tagged.replace(file)
        except OSError:
            log_warn(f"replaygain tag write failed for {file}")
    else:
        err = (proc2.stderr or "").strip()
        log_warn(f"replaygain tag write failed for {file}; RG tags not added. ffmpeg stderr: {err}")


def copy_audio(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


# --------------
# mpv IPC helpers
# --------------

class MpvIPC:
    def __init__(self, path: str, is_windows_pipe: bool):
        self.path = path
        self.is_windows_pipe = is_windows_pipe

    def send(self, payload: dict) -> Optional[str]:
        data = json.dumps(payload, separators=(",", ":"))
        if self.is_windows_pipe:
            try:
                with open(self.path, "r+b", buffering=0) as pipe:
                    pipe.write(data.encode("utf-8") + b"\n")
                    pipe.flush()
                    out = pipe.read()
                    return out.decode("utf-8", errors="ignore")
            except OSError:
                return None
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.path)
                sock.sendall(data.encode("utf-8") + b"\n")
                sock.shutdown(socket.SHUT_WR)
                chunks: List[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="ignore")
            except OSError:
                return None
            finally:
                try:
                    sock.close()
                except OSError:
                    pass


def get_playlist_pos(ipc: MpvIPC) -> Optional[int]:
    resp = ipc.send({"command": ["get_property", "playlist-pos"]})
    if not resp:
        return None
    m = re.search(r'"data"\s*:\s*([^,}]+)', resp)
    if not m:
        return None
    val = m.group(1).strip().strip('"')
    if val == "null" or val == "":
        return None
    try:
        return int(val)
    except ValueError:
        return None


def get_current_rg_track_gain(ipc: MpvIPC) -> Optional[str]:
    resp = ipc.send({"command": ["get_property", "current-tracks/audio/replaygain-track-gain"]})
    if not resp:
        return None
    m = re.search(r'"data"\s*:\s*"([^"]+)"', resp)
    if m:
        return m.group(1)
    m = re.search(r'"data"\s*:\s*([-0-9.]+)', resp)
    if m:
        return f"{m.group(1)} dB"
    return None


def get_current_path(ipc: MpvIPC) -> Optional[str]:
    resp = ipc.send({"command": ["get_property", "path"]})
    if not resp:
        return None
    m = re.search(r'"data"\s*:\s*"([^"]+)"', resp)
    if m:
        return m.group(1)
    return None


def append_to_mpv(ipc: MpvIPC, file: Path, mode: str) -> None:
    ipc.send({"command": ["loadfile", str(file), mode]})


# --------------
# Playback helpers
# --------------

def print_header(mode: str, library: Optional[Path], album_dir: Optional[Path], playlist_file: Optional[Path], total: int, socket_path: str, normalize: bool, album_spread_mode: bool, album_count: int, recent_albums_size: int) -> None:
    rescan_pretty = human_rescan_interval(RANDOM_RESCAN_INTERVAL)
    if mode == "random":
        mode_line = "🔀 Mode: random"
        path_line = f"💾 Library: {library}"
        random_album_line = f"💿 Albums: {album_count}"
        if album_spread_mode:
            random_desc_line = f"🔁 I rotate albums, skip the last {recent_albums_size} of {album_count}, and look for new music every {rescan_pretty}."
        else:
            random_desc_line = f"🎲 Single big shuffle (library has < {ALBUM_SPREAD_THRESHOLD} albums)."
    elif mode == "album":
        mode_line = "🎯 Mode: album"
        path_line = f"💾 Album: {album_dir}"
        random_desc_line = ""
        random_album_line = ""
    else:
        mode_line = "📜 Mode: playlist"
        path_line = f"💾 Playlist: {playlist_file}"
        random_desc_line = ""
        random_album_line = ""

    header_lines = ["\033[35m🎵 mpv music wrapper 🎵\033[0m", "---"]
    header_lines.append(path_line)
    header_lines.append(mode_line)
    if mode == "random":
        header_lines.append(random_desc_line)
        header_lines.append(random_album_line)
    header_lines.append(f"Tracks: {total}")
    header_lines.append("---")
    header_lines.append(f"Socket: {socket_path}")
    header_lines.append(f"Buffer ahead: {BUFFER_AHEAD}")
    if normalize:
        header_lines.append("Normalize: enabled (ReplayGain track)")
    else:
        header_lines.append("Normalize: disabled")

    max_len = 0
    for line in header_lines:
        if line == "---":
            continue
        vlen = visible_len(line)
        if vlen > max_len:
            max_len = vlen
    inner_width = max_len

    print("\033[36m╔" + "═" * (inner_width + 2) + "╗\033[0m", file=sys.stderr)
    is_first = True
    for line in header_lines:
        if line == "---":
            print("\033[36m╟" + "─" * (inner_width + 2) + "╢\033[0m", file=sys.stderr)
            continue
        vlen = visible_len(line)
        pad_len = max(inner_width - vlen, 0)
        left_pad = 0
        if is_first:
            left_pad = pad_len // 2
            pad_len -= left_pad
            is_first = False
        print(f"\033[36m║\033[0m {' ' * left_pad}{line}{' ' * pad_len} \033[36m║\033[0m", file=sys.stderr)
    print("\033[36m╚" + "═" * (inner_width + 2) + "╝\033[0m", file=sys.stderr)


# -----------------
# Track collection
# -----------------

def build_album_map(library: Path) -> Tuple[List[Path], Dict[Path, List[Path]], Dict[Path, int], int]:
    albums: List[Path] = []
    album_track_files: Dict[Path, List[Path]] = {}
    album_track_count: Dict[Path, int] = {}
    total_track_count = 0
    for entry in library.iterdir():
        if entry.is_dir():
            tracks: List[Path] = []
            for root, _, files in os.walk(entry):
                for name in files:
                    f = Path(root) / name
                    if is_audio(f):
                        tracks.append(f)
            if tracks:
                albums.append(entry)
                album_track_files[entry] = tracks
                album_track_count[entry] = len(tracks)
                total_track_count += len(tracks)
    return albums, album_track_files, album_track_count, total_track_count


def gather_random_tracks(library: Path, album_spread_mode: bool, albums: List[Path], album_track_files: Dict[Path, List[Path]]) -> List[Path]:
    if album_spread_mode:
        if not albums:
            die(f"No albums with audio files found under {library}")
        return []
    tracks: List[Path] = []
    for album in albums:
        tracks.extend(album_track_files[album])
    random.shuffle(tracks)
    if not tracks:
        die(f"No audio files found under {library}")
    return tracks


def gather_album_tracks(album_dir: Path) -> List[Path]:
    tracks: List[Path] = []
    for root, _, files in os.walk(album_dir):
        for name in files:
            f = Path(root) / name
            if is_audio(f):
                tracks.append(f)
    tracks.sort()
    if not tracks:
        die("No audio files found in album directory")
    return tracks


def parse_m3u_like(file: Path, dir_path: Path, add_track) -> None:
    with file.open("r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if os.path.isabs(line) or re.match(r"^[A-Za-z]:\\\\", line):
                path = Path(line)
            else:
                path = dir_path / line
            if path.is_file() and is_audio(path):
                add_track(path)
            elif path.is_file():
                log_warn(f"Skipping non-audio entry in playlist: {path}")


def parse_pls(file: Path, dir_path: Path, add_track) -> None:
    with file.open("r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = re.match(r"File[0-9]+=([^\n]+)", line)
            if not m:
                continue
            val = m.group(1)
            if os.path.isabs(val) or re.match(r"^[A-Za-z]:\\\\", val):
                path = Path(val)
            else:
                path = dir_path / val
            if path.is_file() and is_audio(path):
                add_track(path)
            elif path.is_file():
                log_warn(f"Skipping non-audio entry in playlist: {path}")


def parse_cue_minimal(file: Path, dir_path: Path, add_track) -> None:
    with file.open("r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = re.match(r"(?i)^FILE\s+\"([^\"]+)\"", line)
            if not m:
                continue
            val = m.group(1)
            if os.path.isabs(val) or re.match(r"^[A-Za-z]:\\\\", val):
                path = Path(val)
            else:
                path = dir_path / val
            if path.is_file() and is_audio(path):
                add_track(path)
            elif path.is_file():
                log_warn(f"Skipping non-audio entry in playlist: {path}")


def gather_playlist_tracks(file: Path) -> List[Path]:
    dir_path = file.parent.resolve()
    tracks: List[Path] = []

    def add_track(p: Path) -> None:
        tracks.append(p)

    ext = lower_ext(file)
    if ext in ("m3u", "m3u8"):
        parse_m3u_like(file, dir_path, add_track)
    elif ext == "pls":
        parse_pls(file, dir_path, add_track)
    elif ext == "cue":
        parse_cue_minimal(file, dir_path, add_track)
    else:
        die(f"Unsupported playlist extension: {ext}")

    if not tracks:
        die("No playable audio entries found in playlist")
    return tracks


# -----------------
# Normalization prep
# -----------------

def prepare_track(index: int, src: Path, tmp_root: Path, library: Optional[Path], display_root: Path, normalize: bool) -> TrackInfo:
    dst_dir = tmp_root / str(index)
    dst_path = dst_dir / src.name

    copy_audio(src, dst_path)
    strip_id3_if_flac(dst_path)
    strip_embedded_art(dst_path)
    strip_rg_tags_if_possible(dst_path)
    add_replaygain_if_requested(dst_path, normalize)

    album_root = album_root_for_track(src, library)
    cover, cover_meta, cover_detail = select_cover_for_track(src, dst_dir, dst_path, album_root, display_root)
    if cover:
        converted = make_cover_png(cover, dst_dir)
        if converted:
            link_cover(converted, dst_dir)
            cover = converted
        else:
            link_cover(cover, dst_dir)
    return TrackInfo(index=index, source_path=src, staged_path=dst_path, cover_path=cover, cover_meta=cover_meta, cover_detail=cover_detail)


# ------------------
# mpv start/stop
# ------------------

def start_mpv(ipc_path: str, normalize: bool, mpv_additional_args: List[str]) -> subprocess.Popen:
    args = list(mpv_additional_args)
    args.extend(["--force-window=immediate", "--idle=yes", "--keep-open=yes", f"--input-ipc-server={ipc_path}", "--cover-art-auto=exact"])
    if normalize:
        args.append("--replaygain=track")
        args.append("--replaygain-clip=yes")
    else:
        args.append("--replaygain=no")
    proc = subprocess.Popen(["mpv", *args])
    return proc


def wait_for_ipc(path: str, is_windows_pipe: bool, timeout: float = 5.0) -> bool:
    start = time.time()
    if is_windows_pipe:
        while time.time() - start < timeout:
            try:
                with open(path, "r+b", buffering=0):
                    return True
            except OSError:
                time.sleep(0.1)
        return False
    else:
        while time.time() - start < timeout:
            if os.path.exists(path):
                return True
            time.sleep(0.1)
        return False


# -----------------
# Main playback loop
# -----------------

def print_rg_for_pos(pos: int, tracks: List[Path], track_infos: Dict[int, TrackInfo], ipc: MpvIPC, display_root: Path) -> None:
    gain = get_current_rg_track_gain(ipc)
    path = get_current_path(ipc)
    base = Path(path).name if path else "unknown"
    if not gain or gain == "null":
        msg = "ReplayGain[track]: (no RG track gain reported)"
    else:
        msg = f"ReplayGain[track]: {gain}"

    src_path = tracks[pos] if pos < len(tracks) else Path("unknown")
    cover_detail = track_infos.get(pos).cover_detail if pos in track_infos else "[ ] no images found"

    print(f"\n\033[33m[RG]\033[0m {msg} | src: {display_path(src_path, display_root)}", file=sys.stderr)
    print(f"\033[36m[ART]\033[0m candidates:\n{cover_detail}", file=sys.stderr)
    print("----------------------------------------", file=sys.stderr)


def clean_finished(upto: int, last_cleaned: int, tmp_root: Path) -> int:
    if upto - 1 > last_cleaned:
        for i in range(last_cleaned + 1, upto):
            shutil.rmtree(tmp_root / str(i), ignore_errors=True)
        return upto - 1
    return last_cleaned


def main(argv: Sequence[str]) -> None:
    args = parse_args(argv)
    persist_recent_albums = args.persist_recent_albums
    cache_path = resolve_recent_albums_cache_path() if persist_recent_albums else None

    check_dependencies(args.normalize)

    display_root = Path("/")
    if args.mode == "random":
        display_root = Path(args.library).resolve()
    elif args.mode == "album":
        display_root = Path(args.album_dir).resolve()
    elif args.mode == "playlist":
        display_root = Path(args.playlist_file).resolve().parent

    tmp_root = choose_tmp_root()
    pid = os.getpid()
    system = platform.system().lower()
    is_windows = system == "windows"
    if is_windows:
        ipc_path = WINDOWS_PIPE_PREFIX + f"mpv-{pid}"
    else:
        ipc_path = str(DEFAULT_SOCKET_DIR / f"mpv-{pid}.sock")

    mpv_proc = start_mpv(ipc_path, args.normalize, args.mpv_additional_args)
    ipc = MpvIPC(ipc_path, is_windows_pipe=is_windows)
    if not wait_for_ipc(ipc_path, is_windows):
        mpv_proc.terminate()
        die(f"mpv IPC socket did not appear at {ipc_path}")

    # clear playlist
    ipc.send({"command": ["playlist-clear"]})

    planner: Optional[RandomPlanner] = None
    if args.mode == "random":
        planner = RandomPlanner.from_library(Path(args.library))
        if persist_recent_albums and cache_path:
            load_recent_albums_cache(cache_path, planner)
        tracks = planner.tracks
        album_spread_mode = planner.album_spread_mode
        recent_albums_size = planner.recent_albums_size
        total = planner.total_track_count if album_spread_mode else len(tracks)
    elif args.mode == "album":
        tracks = gather_album_tracks(Path(args.album_dir))
        album_spread_mode = False
        recent_albums_size = 0
        total = len(tracks)
    else:
        tracks = gather_playlist_tracks(Path(args.playlist_file))
        album_spread_mode = False
        recent_albums_size = 0
        total = len(tracks)

    print_header(
        mode=args.mode,
        library=Path(args.library) if args.library else None,
        album_dir=Path(args.album_dir) if args.album_dir else None,
        playlist_file=Path(args.playlist_file) if args.playlist_file else None,
        total=total,
        socket_path=ipc_path,
        normalize=args.normalize,
        album_spread_mode=album_spread_mode,
        album_count=len(planner.albums) if planner else 0,
        recent_albums_size=recent_albums_size,
    )

    next_to_prepare = 0
    highest_appended = -1
    current_pos = -1
    last_cleaned = -1
    track_infos: Dict[int, TrackInfo] = {}
    album_by_index: Dict[int, Path] = {}

    def queue_more(total_tracks: int) -> bool:
        nonlocal next_to_prepare, highest_appended, tracks
        appended = False
        target = current_pos + BUFFER_AHEAD
        while highest_appended < target:
            if album_spread_mode:
                assert planner is not None
                if next_to_prepare >= len(tracks):
                    rescan_performed = planner.maybe_refresh_album_map()
                    if rescan_performed and persist_recent_albums and cache_path:
                        save_recent_albums_cache(cache_path, planner.recent_albums)
                    album_choice = choose_album_for_play(planner.albums, list(planner.recent_albums), planner.recent_albums_size)
                    if not album_choice:
                        break
                    track_choice = planner.choose_track_in_album(album_choice)
                    if not track_choice:
                        break
                    album_by_index[next_to_prepare] = album_choice
                    tracks.append(track_choice)
                src = tracks[next_to_prepare]
            else:
                if next_to_prepare >= total_tracks:
                    break
                src = tracks[next_to_prepare]

            info = prepare_track(next_to_prepare, src, tmp_root, Path(args.library) if args.library else None, display_root, args.normalize)
            track_infos[next_to_prepare] = info

            mode = "replace" if highest_appended < 0 else "append-play"
            append_to_mpv(ipc, info.staged_path, mode)
            highest_appended = next_to_prepare
            next_to_prepare += 1
            appended = True
        return appended

    queue_more(total)

    while True:
        time.sleep(POLL_INTERVAL)
        if mpv_proc.poll() is not None:
            break

        pos = get_playlist_pos(ipc)
        if pos is None:
            if album_spread_mode:
                if queue_more(total):
                    continue
                else:
                    break
            else:
                if next_to_prepare >= total:
                    break
                else:
                    queue_more(total)
                    continue

        if pos != current_pos:
            if album_spread_mode and pos in album_by_index:
                planner.recent_albums.append(album_by_index[pos])
            last_cleaned = clean_finished(pos, last_cleaned, tmp_root)
            current_pos = pos
            print_rg_for_pos(pos, tracks, track_infos, ipc, display_root)
            if album_spread_mode:
                if not queue_more(total):
                    break
            else:
                queue_more(total)

    if persist_recent_albums and cache_path and planner is not None:
        save_recent_albums_cache(cache_path, planner.recent_albums)

    shutil.rmtree(tmp_root, ignore_errors=True)
    try:
        if is_windows:
            os.remove(ipc_path)
        else:
            if os.path.exists(ipc_path):
                os.remove(ipc_path)
    except OSError:
        pass
    try:
        mpv_proc.wait(timeout=1)
    except Exception:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
