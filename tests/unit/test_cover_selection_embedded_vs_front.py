from pathlib import Path

import mpv_music_wrapper as mw


def make_candidate(
    name: str,
    width: int,
    height: int,
    size_mb: float,
    bucket: int,
    pref_kw_count: int = 0,
    kw_rank: int = 999,
    scope_rank: int = 0,
    src_type: str = "external",
    is_embedded: bool = False,
    scope: str = "album-root",
) -> mw.CoverCandidate:
    area = width * height
    size_bytes = int(size_mb * 1_000_000)
    return mw.CoverCandidate(
        path=Path(name),
        width=width,
        height=height,
        area=area,
        size_bytes=size_bytes,
        pref_kw_count=pref_kw_count,
        name_token_score=0,
        has_non_front=False,
        bucket=bucket,
        kw_rank=kw_rank,
        scope_rank=scope_rank,
        scope="embedded" if is_embedded else scope,
        src_type=src_type,
        name=name,
        album_tokens=[],
        rel_display=name,
        is_embedded=is_embedded,
    )


def test_embedded_should_not_beat_huge_front_when_both_bucket1():
    # Reproduce the Grease candidate list from the log, preserving order.
    # All externals are scope_rank=1, scope="album-root" from analysis; embedded is scope=embedded, rank 0.
    back = make_candidate("Covers/back.png", 6452, 3172, 116.4, bucket=3, scope_rank=1, scope="album-root")
    book1 = make_candidate("Covers/book1.png", 6396, 3212, 111.4, bucket=3, scope_rank=1, scope="album-root")
    book10 = make_candidate("Covers/book10.png", 6396, 3212, 63.8, bucket=3, scope_rank=1, scope="album-root")
    book11 = make_candidate("Covers/book11.png", 6396, 3212, 62.8, bucket=3, scope_rank=1, scope="album-root")
    book12 = make_candidate("Covers/book12.png", 6396, 3212, 69.6, bucket=3, scope_rank=1, scope="album-root")
    book13 = make_candidate("Covers/book13.png", 6396, 3212, 71.0, bucket=3, scope_rank=1, scope="album-root")
    book14 = make_candidate("Covers/book14.png", 6396, 3212, 70.6, bucket=3, scope_rank=1, scope="album-root")
    book2 = make_candidate("Covers/book2.png", 6396, 3212, 72.9, bucket=3, scope_rank=1, scope="album-root")
    book3 = make_candidate("Covers/book3.png", 6396, 3212, 70.4, bucket=3, scope_rank=1, scope="album-root")
    book4 = make_candidate("Covers/book4.png", 6396, 3212, 72.4, bucket=3, scope_rank=1, scope="album-root")
    book5 = make_candidate("Covers/book5.png", 6396, 3212, 69.0, bucket=3, scope_rank=1, scope="album-root")
    book6 = make_candidate("Covers/book6.png", 6396, 3212, 65.1, bucket=3, scope_rank=1, scope="album-root")
    book7 = make_candidate("Covers/book7.png", 6396, 3212, 70.6, bucket=3, scope_rank=1, scope="album-root")
    book8 = make_candidate("Covers/book8.png", 6396, 3212, 65.2, bucket=3, scope_rank=1, scope="album-root")
    book9 = make_candidate("Covers/book9.png", 6396, 3212, 63.2, bucket=3, scope_rank=1, scope="album-root")
    cd1 = make_candidate("Covers/cd1.png", 2884, 2832, 42.7, bucket=3, scope_rank=1, scope="album-root")
    cd2 = make_candidate("Covers/cd2.png", 2848, 2856, 42.8, bucket=1, pref_kw_count=0, kw_rank=999, scope_rank=1, scope="album-root")
    front = make_candidate("Covers/front.png", 6452, 3172, 115.8, bucket=1, pref_kw_count=1, kw_rank=1, scope_rank=1, scope="album-root")
    inlay_back = make_candidate("Covers/inlay back.png", 2952, 3020, 44.6, bucket=3, scope_rank=1, scope="album-root")
    inlay_front = make_candidate("Covers/inlay front.png", 2976, 3024, 46.7, bucket=1, pref_kw_count=1, kw_rank=1, scope_rank=1, scope="album-root")
    obi = make_candidate("Covers/obi.png", 4260, 3136, 39.9, bucket=3, scope_rank=1, scope="album-root")
    embedded = make_candidate("embedded-cover.png", 500, 500, 0.4, bucket=1, pref_kw_count=1, kw_rank=0, scope_rank=0, src_type="embedded", is_embedded=True, scope="embedded")

    candidates = [
        back,
        book1,
        book10,
        book11,
        book12,
        book13,
        book14,
        book2,
        book3,
        book4,
        book5,
        book6,
        book7,
        book8,
        book9,
        cd1,
        cd2,
        front,
        inlay_back,
        inlay_front,
        obi,
        embedded,
    ]

    best, _, _ = mw.select_best_cover(candidates, [], Path("dummy.flac"), Path("/"), Path("/album"))

    # Expected: front (bucket 1, keyworded, huge) should win over embedded bucket-1 cover.
    assert best is front
