import os
import random
from collections import deque
from pathlib import Path

import pytest

import mpv_music_wrapper as mw


LIB_ENV = "MPV_MUSIC_LIBRARY"


@pytest.mark.skipif(LIB_ENV not in os.environ, reason="MPV_MUSIC_LIBRARY not set")
def test_album_spread_history_avoidance():
    library = Path(os.environ[LIB_ENV])
    assert library.is_dir(), f"Library path not found: {library}"

    albums, album_track_files, album_track_count, total_track_count = mw.build_album_map(library)
    assert albums, "No albums found under library"

    if len(albums) < mw.ALBUM_SPREAD_THRESHOLD:
        pytest.skip(f"Library has < {mw.ALBUM_SPREAD_THRESHOLD} albums; album-spread disabled")

    hist_size = mw.compute_album_history_size(len(albums))
    history: deque[Path] = deque(maxlen=hist_size)

    random.seed(0)
    picks = max(1, 3 * len(albums))

    for _ in range(picks):
        pick = mw.choose_album_for_play(albums, list(history), hist_size)
        assert pick in albums

        blocked = set(list(history)[-hist_size:]) if hist_size > 0 else set()
        if hist_size > 0 and len(blocked) < len(albums):
            # When not all albums are blocked, we should avoid recently played ones.
            assert pick not in blocked

        history.append(pick)
