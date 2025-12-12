import random
from pathlib import Path

import mpv_music_wrapper as mw


class TestAlbumSpreadHelpers:
    def test_compute_recent_albums_size_bounds(self):
        # Below threshold: clamps to total_albums - 1 after applying min/max rules.
        assert mw.compute_recent_albums_size(3) == 2
        # Typical library: respects minimum cap.
        assert mw.compute_recent_albums_size(50) == mw.RECENT_ALBUMS_MIN
        # Very large library: respects maximum cap.
        assert mw.compute_recent_albums_size(5_000) == mw.RECENT_ALBUMS_MAX

    def test_choose_album_avoids_recent_albums(self, monkeypatch):
        albums = [Path(f"Album-{i}") for i in range(4)]
        recent_albums = [albums[0], albums[2]]

        # Deterministic pick: choose first candidate.
        monkeypatch.setattr(random, "choice", lambda seq: seq[0])

        pick = mw.choose_album_for_play(albums, recent_albums, recent_size=2)
        assert pick == albums[1]

    def test_choose_album_when_all_blocked_falls_back(self, monkeypatch):
        albums = [Path(f"Album-{i}") for i in range(3)]
        recent_albums = list(albums)

        # Deterministic pick: choose last element.
        monkeypatch.setattr(random, "choice", lambda seq: seq[-1])

        pick = mw.choose_album_for_play(albums, recent_albums, recent_size=3)
        assert pick == albums[-1]

    def test_choose_album_none_on_empty_list(self):
        pick = mw.choose_album_for_play([], [], recent_size=1)
        assert pick is None
