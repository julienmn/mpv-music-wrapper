import mpv_music_wrapper as mw
from mpv_music_wrapper import CoverCandidate


def make_candidate(
    name,
    bucket,
    area,
    width,
    height,
    pref_kw_count=0,
    name_token_score=0,
    kw_rank=999,
    scope_rank=0,
    is_embedded=False,
):
    return CoverCandidate(
        name=name,
        path=mw.Path(name),
        bucket=bucket,
        pref_kw_count=pref_kw_count,
        name_token_score=name_token_score,
        kw_rank=kw_rank,
        scope="embedded" if is_embedded else "disc",
        scope_rank=scope_rank,
        area=area,
        size_bytes=0,
        is_embedded=is_embedded,
        width=width,
        height=height,
        has_non_front=False,
        src_type="embedded" if is_embedded else "file",
        album_tokens=[],
        rel_display=name,
    )


def test_cover_selection_poppy_negative_spaces_current_behavior():
    candidates = [
        make_candidate("Case-Front.jpg", bucket=2, area=20_903_960, width=6_787, height=3_080, pref_kw_count=1, kw_rank=1, scope_rank=1),
        make_candidate("Sealed-Front.jpg", bucket=1, area=10_486_848, width=3_396, height=3_088, pref_kw_count=1, kw_rank=1, scope_rank=1),
        make_candidate("Poppy - Negative Spaces.jpg", bucket=2, area=1_440_000, width=1_200, height=1_200, name_token_score=3, kw_rank=4, scope_rank=1),
        make_candidate("embedded-cover.png", bucket=1, area=360_000, width=600, height=600, pref_kw_count=1, kw_rank=0, is_embedded=True, scope_rank=0),
    ]
    best, _, _ = mw.select_best_cover(
        candidates,
        detail_lines=[],
        track=mw.Path("track.flac"),
        display_root=mw.Path("/"),
        base_root=None,
    )
    # Desired: pick the squarer Sealed-Front over the very wide Case-Front.
    assert best.name == "Sealed-Front.jpg"


def test_cover_selection_green_nuns_current_behavior():
    candidates = [
        make_candidate("image1.jpeg", bucket=2, area=250_000, width=500, height=500, scope_rank=1),
        make_candidate("image2.jpeg", bucket=2, area=320_500, width=641, height=500, scope_rank=1),
        make_candidate("image3.jpeg", bucket=2, area=504_500, width=1_009, height=500, scope_rank=1),
        make_candidate("image4.jpeg", bucket=2, area=253_500, width=507, height=500, scope_rank=1),
    ]
    best, _, _ = mw.select_best_cover(
        candidates,
        detail_lines=[],
        track=mw.Path("track.flac"),
        display_root=mw.Path("/"),
        base_root=None,
    )
    # Desired: pick the most square image (image1) over the wider/larger options.
    assert best.name == "image1.jpeg"
