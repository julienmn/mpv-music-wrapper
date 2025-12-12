#!/usr/bin/env python3
"""
Dump cover candidate metadata for a given track path.

Usage:
  python tests/tools/dump_cover_candidates.py /path/to/track.flac

Outputs JSON lines with the fields needed to reproduce cover selection in tests.
Requires ffprobe/ffmpeg to be available (for metadata and embedded extraction).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import mpv_music_wrapper as mw


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tests/tools/dump_cover_candidates.py /path/to/track.flac", file=sys.stderr)
        return 1

    track = Path(sys.argv[1]).resolve()
    if not track.is_file():
        print(f"Track not found: {track}", file=sys.stderr)
        return 1

    album_root = track.parent.parent if track.parent.name.lower().startswith("cd") else track.parent
    dir_path = track.parent
    tmp_dir = Path(mw.choose_tmp_root())  # temp dir for embedded extraction

    is_multi = album_root and dir_path != album_root and str(dir_path).startswith(str(album_root))

    candidates_paths, embedded_path = mw.gather_image_candidates(dir_path, album_root, is_multi, track, tmp_dir)
    candidates, detail_lines = mw.analyze_candidates(
        candidates_paths=candidates_paths,
        embedded_path=embedded_path,
        track=track,
        album_root=album_root,
        dir_path=dir_path,
        base_root=album_root if album_root else dir_path,
        display_root=Path("/"),
    )

    print(f"[info] track={track}")
    print(f"[info] dir={dir_path}")
    print(f"[info] album_root={album_root}")
    print(f"[info] embedded={embedded_path}")
    print(f"[info] candidates={len(candidates)}")

    for c in candidates:
        print(json.dumps({
            "name": c.name,
            "path": str(c.path),
            "bucket": c.bucket,
            "pref_kw_count": c.pref_kw_count,
            "name_token_score": c.name_token_score,
            "kw_rank": c.kw_rank,
            "scope": c.scope,
            "scope_rank": c.scope_rank,
            "area": c.area,
            "size_bytes": c.size_bytes,
            "is_embedded": c.is_embedded,
        }))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
