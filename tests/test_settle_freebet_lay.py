"""Settlement P&L for free bets and lay bets.

- Free bet (source='offer', stake NOT returned): a loss costs £0, a win pays
  profit only (odds-1)*stake.
- Lay bet ('Lay (Bet Against)'): a loss costs the LIABILITY (stake*(odds-1)),
  a win pays the backer stake.
- A normal back bet is unchanged (won (odds-1)*stake, lost -stake).
"""
from __future__ import annotations

from wca.ledger.store import record_bet, settle_bet
from wca.ledger.reports import _bets_df


def _pl(db, bet_id):
    df = _bets_df(db)
    return float(df[df["id"] == bet_id]["settled_pl"].iloc[0])


def test_free_bet_loss_costs_zero(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "A",
                     "virginbet", 5.0, 10.0, source="offer", db_path=db)
    settle_bet(bid, "lost", db_path=db)
    assert _pl(db, bid) == 0.0  # free bet: stake not returned, loss = £0


def test_free_bet_win_pays_profit_only(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "A",
                     "virginbet", 10.0, 1.0, source="offer", db_path=db)
    settle_bet(bid, "won", db_path=db)
    assert _pl(db, bid) == 9.0  # (10-1)*1 profit only


def test_lay_loss_costs_liability(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Lay (Bet Against)",
                     "A", "Betfair", 2.46, 60.0, source="punt", db_path=db)
    settle_bet(bid, "lost", db_path=db)
    assert abs(_pl(db, bid) - (-87.60)) < 1e-6  # liability 60*(2.46-1)


def test_lay_win_pays_backer_stake(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Lay (Bet Against)",
                     "A", "Betfair", 2.46, 60.0, source="punt", db_path=db)
    settle_bet(bid, "won", db_path=db)
    assert _pl(db, bid) == 60.0  # lay wins the backer's stake


def test_normal_back_bet_unchanged(tmp_path):
    db = str(tmp_path / "wca.db")
    won = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "A",
                     "bet365", 2.0, 10.0, source="model", db_path=db)
    lost = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "B",
                      "bet365", 2.0, 10.0, source="model", db_path=db)
    settle_bet(won, "won", db_path=db)
    settle_bet(lost, "lost", db_path=db)
    assert _pl(db, won) == 10.0
    assert _pl(db, lost) == -10.0
