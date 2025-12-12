import os
import os
import random
from pathlib import Path

import pytest

import mpv_music_wrapper as mw


LIB_ENV = "MPV_MUSIC_LIBRARY"


@pytest.mark.skipif(LIB_ENV not in os.environ, reason="MPV_MUSIC_LIBRARY not set")
def test_album_spread_history_avoidance():
    library = Path(os.environ[LIB_ENV])
    assert library.is_dir(), f"Library path not found: {library}"

    planner = mw.RandomPlanner.from_library(library)
    assert planner.albums, "No albums found under library"

    if not planner.album_spread_mode:
        pytest.skip(f"Library has < {mw.ALBUM_SPREAD_THRESHOLD} albums; album-spread disabled")

    random.seed(0)
    picks = max(1, 3 * len(planner.albums))

    print(
        f"[integration] library={library} albums={len(planner.albums)} "
        f"tracks={planner.total_track_count} hist_size={planner.album_history_size} picks={picks}"
    )

    for _ in range(picks):
        planner.maybe_refresh_album_map()
        pick = mw.choose_album_for_play(planner.albums, list(planner.album_history), planner.album_history_size)
        assert pick in planner.albums

        blocked = set(list(planner.album_history)[-planner.album_history_size:]) if planner.album_history_size > 0 else set()
        if planner.album_history_size > 0 and len(blocked) < len(planner.albums):
            # When not all albums are blocked, we should avoid recently played ones.
            assert pick not in blocked

        planner.album_history.append(pick)
