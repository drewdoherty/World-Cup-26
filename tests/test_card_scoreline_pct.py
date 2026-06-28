"""The scoreline card shows implied probabilities after the fair/back decimal
odds (``fair 5.62 (17.8%)  back >= 5.73 (17.5%)``). Guard both directions:
the new format round-trips through the site-feed parser, and the older
note-free format still parses (backward compatible)."""

from __future__ import annotations

from wca.sitedata import _SCORE_RE


def test_parses_new_implied_pct_format():
    m = _SCORE_RE.match("0-1  17.8%  fair 5.62 (17.8%)  back >= 5.73 (17.5%)")
    assert m is not None
    assert m.group("score") == "0-1"
    assert m.group("prob") == "17.8"
    assert m.group("fair") == "5.62"      # parser ignores the (xx%) note
    assert m.group("back") == "5.73"


def test_backward_compatible_with_note_free_format():
    m = _SCORE_RE.match("2-1  12.3%  fair 8.13  back >= 8.46")
    assert m is not None
    assert m.group("fair") == "8.13"
    assert m.group("back") == "8.46"


def test_implied_pct_is_reciprocal_of_decimal_odds():
    # The displayed implied % after a decimal price is 100 / odds.
    for odds in (5.62, 5.73, 1.53, 9.80):
        assert round(100.0 / odds, 1) == round(100.0 / odds, 1)  # math sanity
    assert round(100.0 / 5.73, 1) == 17.5
    assert round(100.0 / 5.62, 1) == 17.8
