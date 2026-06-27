"""Tests for the Polymarket exposure/trim proposer (:mod:`wca.pmtrim`)."""
from __future__ import annotations

from wca.pmtrim import (
    Action,
    Position,
    classify,
    format_proposals,
    ping_proposals,
    propose,
)


def _pos(dec, model_p, stake=100.0, market="Will X reach the Round of 16?", sel="Yes"):
    return Position(market=market, selection=sel, stake=stake, decimal_odds=dec, model_prob=model_p)


def test_implied_and_edge():
    p = _pos(2.0, 0.6)
    assert abs(p.implied_prob - 0.5) < 1e-9
    assert abs(p.edge - 0.2) < 1e-9  # 0.6*2 - 1
    assert abs(p.moneyline_distance - 0.0) < 1e-9


def test_no_model_prob_is_review():
    pr = classify(_pos(2.0, None))
    assert pr.action == Action.REVIEW
    assert pr.suggested_stake == 100.0


def test_negative_edge_trims_to_zero():
    # model 0.45 at 2.0 -> edge -0.1 -> full exit.
    pr = classify(_pos(2.0, 0.45))
    assert pr.action == Action.TRIM
    assert pr.suggested_stake == 0.0


def test_longshot_trims_even_when_positive_edge():
    # 10% implied (decimal 10) but +EV; rule deprioritises longshots -> exit.
    pr = classify(_pos(10.0, 0.15))
    assert pr.action == Action.TRIM
    assert pr.suggested_stake == 0.0
    assert "longshot" in pr.reason.lower()


def test_converged_edge_banks_half():
    # 55% implied, edge +3% (< 5% min) -> trim by half.
    pr = classify(_pos(1.0 / 0.55, 0.567), trim_fraction=0.5)
    assert pr.action == Action.TRIM
    assert abs(pr.suggested_stake - 50.0) < 1.0
    assert pr.stake_change < 0


def test_near_moneyline_large_edge_is_add():
    # 60% implied (in band), edge ~+20% -> ADD.
    pr = classify(_pos(1.0 / 0.60, 0.72))
    assert pr.action == Action.ADD


def test_healthy_edge_outside_band_keeps():
    # 28% implied (outside moneyline band, not a longshot), big edge -> KEEP.
    pr = classify(_pos(1.0 / 0.28, 0.383))
    assert pr.action == Action.KEEP


def test_propose_orders_trims_and_adds_first():
    positions = [
        _pos(1.53, 0.70, market="Will A reach the Round of 16?"),   # keep, small edge
        _pos(10.0, 0.15, market="Will B win the 2026 FIFA World Cup?"),  # trim longshot
        _pos(1.0 / 0.60, 0.72, market="Will C advance to the knockout stages?"),  # add
    ]
    out = propose(positions)
    actions = [p.action for p in out]
    # TRIM and ADD come before KEEP.
    assert actions.index(Action.TRIM) < actions.index(Action.KEEP)
    assert actions.index(Action.ADD) < actions.index(Action.KEEP)


def test_format_contains_actions_and_warning():
    out = propose([_pos(10.0, 0.15), _pos(1.53, 0.70)])
    text = format_proposals(out)
    assert "TRIM" in text
    assert "verify live" in text.lower()


def test_format_empty():
    assert "No open Polymarket positions" in format_proposals([])


def test_ping_dry_run_does_not_send(capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    # dry_run wins even when creds are present.
    sent = ping_proposals("hello", dry_run=True)
    assert sent is False
    assert "not sent" in capsys.readouterr().out


def test_ping_without_token_does_not_send(capsys, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sent = ping_proposals("hello", token=None, chat_id=None, dry_run=False)
    assert sent is False
    assert "not sent" in capsys.readouterr().out
