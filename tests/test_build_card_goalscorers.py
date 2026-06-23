"""Tests for the /goalscorers card build guards in scripts/wca_build_card.py.

Regression: the fast 30-min `buildcard --skip-scorers` job rewrote
data/goalscorers_latest.md with an empty scorer set, so every fixture rendered
"no sportsbook scorer market available" even when markets existed. The fix:
- the scorer-less job must NOT (re)build the card (want_goalscorers_card)
- the card is only WRITTEN when a real scorer market came back (has_scorer_markets)
so a quiet window / API miss preserves the last good card.

scripts/ is not a package; load the module by path (like test_pm_exposure).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_build_card.py"
_spec = importlib.util.spec_from_file_location("wca_build_card", _SCRIPT)
wbc = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(wbc)  # type: ignore[union-attr]


class _DF:
    """Minimal stand-in for a DataFrame: just an `.empty` attribute."""

    def __init__(self, empty: bool) -> None:
        self.empty = empty


# -- want_goalscorers_card: which runs may (re)build the scorer card -----------

def test_fast_skip_scorers_job_does_not_rebuild():
    # the 30-min buildcard --skip-scorers job must leave the card alone
    assert wbc.want_goalscorers_card("data/goalscorers_latest.md", 5, True, False) is False


def test_goalscorers_only_refresh_rebuilds_even_with_skip_scorers():
    assert wbc.want_goalscorers_card("data/goalscorers_latest.md", 5, True, True) is True
    assert wbc.want_goalscorers_card("data/goalscorers_latest.md", 5, False, True) is True


def test_normal_full_build_rebuilds():
    assert wbc.want_goalscorers_card("data/goalscorers_latest.md", 5, False, False) is True


def test_no_output_or_zero_fixtures_never_builds():
    assert wbc.want_goalscorers_card("", 5, False, False) is False
    assert wbc.want_goalscorers_card("data/goalscorers_latest.md", 0, False, False) is False


# -- has_scorer_markets: gate the WRITE so empties never clobber a good card ----

def test_no_markets_means_do_not_write():
    assert wbc.has_scorer_markets({}) is False
    assert wbc.has_scorer_markets(None) is False
    # all fixtures came back empty (quiet window / API miss) -> preserve
    assert wbc.has_scorer_markets({"e1": _DF(True), "e2": _DF(True)}) is False


def test_one_real_market_means_write():
    assert wbc.has_scorer_markets({"e1": _DF(True), "e2": _DF(False)}) is True
