"""Pure unit tests for the adaptive poll-scheduling decision logic.

No network, no real clock, no daemon import -- everything is driven by
injected ISO timestamps so the behaviour is fully deterministic.
"""
from __future__ import annotations

from wca.pollsched import PollPolicy, estimate_monthly_calls, next_poll_delay

# A fixed reference "now" used across tests.
NOW = "2026-06-11T18:00:00+00:00"


def _policy(**overrides):
    p = PollPolicy()
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def test_live_match_uses_in_game_cadence():
    # Kickoff 30 min ago -> match is live (< 130 min duration).
    kickoffs = ["2026-06-11T17:30:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "in_game"
    assert delay == PollPolicy().in_game_seconds


def test_kickoff_in_8_minutes_uses_pre_close():
    kickoffs = ["2026-06-11T18:08:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "pre_close"
    assert delay == PollPolicy().pre_close_seconds


def test_idle_when_nothing_imminent_or_live():
    # Kickoff in 3 hours: not live, not within the 10-min pre-close window.
    kickoffs = ["2026-06-11T21:00:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "idle"
    assert delay == PollPolicy().idle_seconds


def test_empty_kickoffs_idle():
    delay, reason = next_poll_delay(NOW, [], quota_remaining=1000, policy=_policy())
    assert reason == "idle"
    assert delay == PollPolicy().idle_seconds


def test_quota_reserve_blocks_everything_including_live_match():
    # Quota below min_reserve (60) -> hard stop even during a live match.
    kickoffs = ["2026-06-11T17:30:00+00:00"]  # live
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=50, policy=_policy())
    assert reason == "quota-reserve"
    assert delay == PollPolicy().low_quota_idle_seconds  # 6h


def test_quota_reserve_blocks_pre_close_too():
    # Closes are sacred *above* the reserve, but the hard reserve overrides
    # even a pre-close window.
    kickoffs = ["2026-06-11T18:08:00+00:00"]  # pre-close
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=10, policy=_policy())
    assert reason == "quota-reserve"
    assert delay == PollPolicy().low_quota_idle_seconds


def test_low_quota_outside_close_window_uses_low_quota_idle():
    # Quota 150 (< 200 but > 60), idle situation -> throttled to low_quota_idle.
    kickoffs = ["2026-06-11T21:00:00+00:00"]  # 3h out, idle
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=150, policy=_policy())
    assert reason == "low_quota_idle"
    assert delay == PollPolicy().low_quota_idle_seconds


def test_low_quota_still_allows_pre_close_poll():
    # Quota 150, kickoff in 8 min -> closing line is sacred above reserve.
    kickoffs = ["2026-06-11T18:08:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=150, policy=_policy())
    assert reason == "pre_close"
    assert delay == PollPolicy().pre_close_seconds


def test_low_quota_live_match_throttled():
    # Live match but low quota and not pre-close -> throttle in-game polling.
    kickoffs = ["2026-06-11T17:30:00+00:00"]  # live
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=150, policy=_policy())
    assert reason == "low_quota_idle"
    assert delay == PollPolicy().low_quota_idle_seconds


def test_malformed_kickoff_is_skipped():
    # One garbage entry + one valid pre-close kickoff: the valid one wins,
    # the garbage one does not crash anything.
    kickoffs = ["not-a-timestamp", "2026-06-11T18:08:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "pre_close"


def test_all_malformed_kickoffs_idle():
    kickoffs = ["garbage", "", "2026-13-99T99:99:99"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "idle"


def test_naive_timestamp_treated_as_utc():
    # No offset on either side -> both interpreted as UTC, kickoff is live.
    delay, reason = next_poll_delay(
        "2026-06-11T18:00:00",
        ["2026-06-11T17:30:00"],
        quota_remaining=1000,
        policy=_policy(),
    )
    assert reason == "in_game"


def test_z_suffix_timestamp_parsed():
    delay, reason = next_poll_delay(
        "2026-06-11T18:00:00Z",
        ["2026-06-11T18:08:00Z"],
        quota_remaining=1000,
        policy=_policy(),
    )
    assert reason == "pre_close"


def test_quota_none_does_not_trigger_budget_guard():
    # Unknown quota -> behave purely on cadence (idle here).
    kickoffs = ["2026-06-11T21:00:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=None, policy=_policy())
    assert reason == "idle"
    assert delay == PollPolicy().idle_seconds


def test_match_just_ended_is_not_live():
    # Kickoff 131 min ago with 130-min duration -> no longer live -> idle.
    kickoffs = ["2026-06-11T15:49:00+00:00"]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "idle"


def test_kickoff_exactly_now_is_live():
    # now == kickoff is the start of the live window (inclusive lower bound).
    kickoffs = [NOW]
    delay, reason = next_poll_delay(NOW, kickoffs, quota_remaining=1000, policy=_policy())
    assert reason == "in_game"


# --- estimate_monthly_calls -------------------------------------------------

def test_estimate_monthly_calls_keys_and_idle_baseline():
    est = estimate_monthly_calls(0.0, PollPolicy())
    assert set(est.keys()) == {"idle_calls", "in_game_calls", "total"}
    # No matches -> no in-game calls, total == idle baseline.
    assert est["in_game_calls"] == 0.0
    assert est["total"] == est["idle_calls"]
    assert est["idle_calls"] > 0


def test_estimate_in_game_grows_with_matches_per_day():
    policy = PollPolicy()
    low = estimate_monthly_calls(1.0, policy)
    high = estimate_monthly_calls(4.0, policy)
    assert high["in_game_calls"] > low["in_game_calls"]
    assert high["total"] > low["total"]


def test_estimate_in_game_calls_scale_linearly():
    policy = PollPolicy()
    one = estimate_monthly_calls(1.0, policy)["in_game_calls"]
    three = estimate_monthly_calls(3.0, policy)["in_game_calls"]
    assert abs(three - 3.0 * one) < 1e-6


def test_idle_never_sleeps_past_pre_close_window():
    """Regression: idle delay must be capped so the daemon wakes when the
    pre-close window opens, not an hour later mid-match."""
    from wca.pollsched import PollPolicy, next_poll_delay

    pol = PollPolicy()
    # Kickoff 13 minutes away: outside the 10-min pre-close window, so the
    # tier is idle — but a full 3600s sleep would miss the close. The delay
    # must be ~180s (13min - 10min window).
    delay, reason = next_poll_delay(
        "2026-06-11T18:47:00", ["2026-06-11T19:00:00"], 10000, pol)
    assert reason == "idle-capped-to-close"
    assert 150 <= delay <= 200

    # Far from any kickoff: plain idle.
    delay2, reason2 = next_poll_delay(
        "2026-06-11T10:00:00", ["2026-06-11T19:00:00"], 10000, pol)
    assert reason2 == "idle"
    assert delay2 == pol.idle_seconds

    # Pre-close window further away than a full idle cycle: not capped.
    delay3, reason3 = next_poll_delay(
        "2026-06-11T17:00:00", ["2026-06-11T19:00:00"], 10000, pol)
    assert reason3 == "idle"
    assert delay3 == pol.idle_seconds

    # 45 minutes out: capped to wake at the window opening (~35 min).
    delay4, reason4 = next_poll_delay(
        "2026-06-11T18:15:00", ["2026-06-11T19:00:00"], 10000, pol)
    assert reason4 == "idle-capped-to-close"
    assert 2000 <= delay4 <= 2200
