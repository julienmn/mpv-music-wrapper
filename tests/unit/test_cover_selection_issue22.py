from pathlib import Path

import mpv_music_wrapper as mw


def make_candidate(
    name: str,
    width: int,
    height: int,
    size_mb: float,
    bucket: int,
    name_token_score: int = 0,
) -> mw.CoverCandidate:
    area = width * height
    size_bytes = int(size_mb * 1_000_000)
    return mw.CoverCandidate(
        path=Path(name),
        width=width,
        height=height,
        area=area,
        size_bytes=size_bytes,
        pref_kw_count=0,
        name_token_score=name_token_score,
        has_non_front=False,
        bucket=bucket,
        kw_rank=999,
        scope_rank=0,
        scope="disc",
        src_type="external",
        name=name,
        album_tokens=[],
        rel_display=name,
        is_embedded=False,
    )


def test_issue22_album_named_cover_should_win_over_booklet():
    # Fixtures from issue #22
    cover = make_candidate(
        "Cat Stevens - Tea for the Tillerman.jpg",
        width=584,
        height=588,
        size_mb=0.1,
        bucket=1,
        name_token_score=6,
    )
    disc = make_candidate("Artwork/Disc.jpg", width=1433, height=1394, size_mb=0.4, bucket=3)
    booklet4 = make_candidate("Artwork/Booklet 04.jpg", width=2848, height=1412, size_mb=0.9, bucket=3)
    booklet3 = make_candidate("Artwork/Booklet 03.jpg", width=2837, height=1412, size_mb=0.6, bucket=3)
    booklet2 = make_candidate("Artwork/Booklet 02.jpg", width=2832, height=1420, size_mb=0.8, bucket=3)
    booklet1 = make_candidate("Artwork/Booklet 01.jpg", width=2812, height=1400, size_mb=1.2, bucket=3)
    back = make_candidate("Artwork/Back.jpg", width=1766, height=1358, size_mb=0.8, bucket=3)

    candidates = [cover, disc, booklet4, booklet3, booklet2, booklet1, back]

    best, _, _ = mw.select_best_cover(candidates, [], Path("dummy.flac"), Path("/"), Path("/album"))

    # Expected winner: album-named cover (bucket 1, name_token_score>0) over booklet pages (bucket 3).
    assert best is cover
