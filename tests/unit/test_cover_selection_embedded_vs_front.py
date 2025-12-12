from pathlib import Path

import mpv_music_wrapper as mw


def make_candidate(
    name: str,
    width: int,
    height: int,
    size_mb: float,
    bucket: int,
    pref_kw_count: int = 0,
    scope_rank: int = 0,
    src_type: str = "external",
    is_embedded: bool = False,
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
        kw_rank=999,
        scope_rank=scope_rank,
        scope="embedded" if is_embedded else "disc",
        src_type=src_type,
        name=name,
        album_tokens=[],
        rel_display=name,
        is_embedded=is_embedded,
    )


def test_embedded_should_not_beat_huge_front_when_both_bucket1():
    # Embedded: small, bucket 1 via keyword, scope embedded.
    embedded = make_candidate(
        "EMBEDDED",
        width=500,
        height=500,
        size_mb=0.4,
        bucket=1,
        pref_kw_count=1,
        scope_rank=0,
        src_type="embedded",
        is_embedded=True,
    )
    # Front cover: large, bucket 1 via keyword, scope album-root (higher rank than embedded).
    front = make_candidate(
        "Covers/front.png",
        width=6452,
        height=3172,
        size_mb=115.8,
        bucket=1,
        pref_kw_count=1,
        scope_rank=1,
        src_type="external",
        is_embedded=False,
    )

    # Order: front first (likely gathered before embedded), then embedded appended last, matching observed behavior.
    candidates = [front, embedded]
    best, _, _ = mw.select_best_cover(candidates, [], Path("dummy.flac"), Path("/"), Path("/album"))

    # Expected: front should win due to vastly larger area when both are bucket 1 keyworded.
    assert best is front
