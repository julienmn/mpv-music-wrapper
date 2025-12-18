#!/usr/bin/env python3
"""
Dump cover candidate metadata for a given track path.

Usage:
  python tests/tools/dump_cover_candidates.py [--library /path/to/library] /path/to/track.flac

Outputs JSON lines with the fields needed to reproduce cover selection in tests,
using the current cover-analysis logic. Requires ffprobe/ffmpeg to be available
for metadata and embedded extraction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import mpv_music_wrapper as mw


def main() -> int:
    args = sys.argv[1:]
    library = None
    if len(args) >= 2 and args[0] == "--library":
        library = Path(args[1]).expanduser().resolve()
        args = args[2:]

    if len(args) != 1:
        print("Usage: python tests/tools/dump_cover_candidates.py [--library /path/to/library] /path/to/track.flac", file=sys.stderr)
        return 1

    track = Path(args[0]).resolve()
    if not track.is_file():
        print(f"Track not found: {track}", file=sys.stderr)
        return 1

    album_root = mw.album_root_for_track(track, library) or track.parent
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
        print(
            json.dumps(
                {
                    "name": c.name,
                    "path": str(c.path),
                    "bucket": c.bucket,
                    "pref_kw_count": c.pref_kw_count,
                    "name_token_score": c.name_token_score,
                    "kw_rank": c.kw_rank,
                    "scope": c.scope,
                    "scope_rank": c.scope_rank,
                    "area": c.area,
                    "width": c.width,
                    "height": c.height,
                    "size_bytes": c.size_bytes,
                    "overlap_ratio": getattr(c, "overlap_ratio", 0.0),
                    "is_embedded": c.is_embedded,
                }
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
