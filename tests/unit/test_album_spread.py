import random
from pathlib import Path

import mpv_music_wrapper as mw


class TestAlbumSpreadHelpers:
    def test_compute_album_history_size_bounds(self):
        # Below threshold: clamps to total_albums - 1 after applying min/max rules.
        assert mw.compute_album_history_size(3) == 2
        # Typical library: respects minimum cap.
        assert mw.compute_album_history_size(50) == mw.ALBUM_HISTORY_MIN
        # Very large library: respects maximum cap.
        assert mw.compute_album_history_size(5_000) == mw.ALBUM_HISTORY_MAX

    def test_choose_album_avoids_recent_history(self, monkeypatch):
        albums = [Path(f"Album-{i}") for i in range(4)]
        history = [albums[0], albums[2]]

        # Deterministic pick: choose first candidate.
        monkeypatch.setattr(random, "choice", lambda seq: seq[0])

        pick = mw.choose_album_for_play(albums, history, hist_size=2)
        assert pick == albums[1]

    def test_choose_album_when_all_blocked_falls_back(self, monkeypatch):
        albums = [Path(f"Album-{i}") for i in range(3)]
        history = list(albums)

        # Deterministic pick: choose last element.
        monkeypatch.setattr(random, "choice", lambda seq: seq[-1])

        pick = mw.choose_album_for_play(albums, history, hist_size=3)
        assert pick == albums[-1]

    def test_choose_album_none_on_empty_list(self):
        pick = mw.choose_album_for_play([], [], hist_size=1)
        assert pick is None
