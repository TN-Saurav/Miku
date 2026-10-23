import pytest

from miku.matching import confident_match, match_score, rank_tracks
from miku.models import Track


def _track(title, artist="Artist", *, official=None):
    return Track("jamendo", title.casefold().replace(" ", "-"), title, artist,
                 official_source=official)


def test_exact_title_and_artist_get_a_confident_automatic_match():
    requested = _track("Shape of You", "Ed Sheeran")
    unrelated = _track("Shape of Blue", "Different Artist")

    assert confident_match("Shape of You Ed Sheeran", [unrelated, requested]) == requested


def test_cover_and_karaoke_versions_are_penalized_without_user_request():
    original = _track("Kalyani", "Artist")
    cover = _track("Kalyani cover", "Fan singer")
    karaoke = _track("Kalyani karaoke instrumental", "Karaoke channel")

    ranked = rank_tracks("Kalyani", [cover, karaoke, original])

    assert ranked[0] == original
    assert match_score("Kalyani", cover) < match_score("Kalyani", original)
    assert match_score("Kalyani", karaoke) < match_score("Kalyani", original)


@pytest.mark.parametrize("variant", [
    "live", "remix", "instrumental", "slowed", "sped up", "nightcore", "acoustic", "lyrics"
])
def test_unrequested_variants_receive_a_lower_rank(variant):
    original = _track("Kalyani", "Artist")
    altered = _track(f"Kalyani {variant}", "Artist")

    assert match_score("Kalyani", altered) < match_score("Kalyani", original)


def test_explicit_cover_or_remix_request_respects_the_requested_variant():
    cover = _track("Kalyani cover", "Artist")
    original = _track("Kalyani", "Artist")
    remix = _track("Kalyani remix", "Artist")

    assert rank_tracks("Kalyani cover", [original, cover])[0] == cover
    assert rank_tracks("Kalyani remix", [original, remix])[0] == remix


def test_verified_official_source_breaks_an_otherwise_equal_metadata_tie():
    unverified = _track("Kalyani", "Artist", official=None)
    verified = _track("Kalyani", "Artist", official=True)

    assert rank_tracks("Kalyani", [unverified, verified])[0] == verified
    assert confident_match("Kalyani", [unverified, verified]) is None


def test_equal_title_matches_are_ambiguous_and_require_manual_selection():
    first = _track("Kalyani", "Artist One")
    second = _track("Kalyani", "Artist Two")

    assert confident_match("Kalyani", [first, second]) is None
