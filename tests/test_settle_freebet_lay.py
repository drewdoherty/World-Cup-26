"""Settlement P&L for free bets and lay bets.

- Free bet (source='offer', stake NOT returned): a loss costs £0, a win pays
  profit only (odds-1)*stake.
- Lay bet ('Lay (Bet Against)'): a loss costs the LIABILITY (stake*(odds-1)),
  a win pays the backer stake.
- A normal back bet is unchanged (won (odds-1)*stake, lost -stake).

Regression: the lay sniff used to be a substring check (`"lay" in label`), so
free-bet accas whose label contains "player"/"play" (also "overlay", "parlay",
"replay", "display") were mis-flagged as lays and charged a lay liability on a
loss instead of £0. The whole-word match plus the void->free->lay->back order in
``store.settled_pl`` fix that, and all three settle paths (store.settle_bet, the
bot /settle command, the wca_settle.py CLI) share it so they cannot disagree.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3

import wca.bot.app as app
from wca.ledger import store
from wca.ledger.store import is_lay_bet, record_bet, settle_bet, settled_pl
from wca.ledger.reports import _bets_df

_SETTLE_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "wca_settle.py",
)


def _pl(db, bet_id):
    df = _bets_df(db)
    return float(df[df["id"] == bet_id]["settled_pl"].iloc[0])


def _settled_pl(db, bet_id):
    """Read settled_pl straight from the DB (works for every settle path)."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return float(
            con.execute(
                "SELECT settled_pl FROM bets WHERE id = ?", (bet_id,)
            ).fetchone()["settled_pl"]
        )
    finally:
        con.close()


def _load_settle():
    spec = importlib.util.spec_from_file_location("wca_settle", _SETTLE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# --------------------------------------------------------------------------- #
# Regression: "player"/"play"-style labels must NOT trip the lay sniff.
# --------------------------------------------------------------------------- #
def test_is_lay_bet_word_boundary():
    # Genuine lay markets are detected.
    assert is_lay_bet("Lay (Bet Against)", "England") is True
    assert is_lay_bet("Match Odds", "Lay A") is True
    # Labels that merely CONTAIN the letters l-a-y are not lays.
    for market, selection in [
        ("Treble", "Player 1+ SOT"),
        ("Acca", "England HT/2H + player SOT"),
        ("Match Odds", "overlay special"),
        ("Parlay", "5 legs"),
        ("Replay", "A"),
        ("Display board", "A"),
    ]:
        assert is_lay_bet(market, selection) is False, (market, selection)


def test_settled_pl_resolution_order_free_beats_lay():
    # void is always £0 regardless of flags.
    assert settled_pl("void", 60.0, 2.46, is_free=True, is_lay=True) == 0.0
    # free bet is resolved BEFORE lay: a free-bet loss costs £0, never a lay
    # liability — this is the exact bug the reorder fixes.
    assert settled_pl("lost", 9.0, 4.5, is_free=True, is_lay=True) == 0.0
    assert settled_pl("won", 1.0, 10.0, is_free=True) == 9.0  # profit only
    # genuine lay still books the liability on a loss, backer stake on a win.
    assert abs(settled_pl("lost", 60.0, 2.46, is_lay=True) - (-87.60)) < 1e-9
    assert settled_pl("won", 60.0, 2.46, is_lay=True) == 60.0
    # plain back bet.
    assert settled_pl("lost", 10.0, 2.0) == -10.0
    assert settled_pl("won", 10.0, 2.0) == 10.0


def test_store_free_bet_player_acca_loss_is_zero(tmp_path):
    """store.settle_bet: the seed id 99 shape ('Treble — Player 1+ SOT')."""
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "England vs Wales",
                     "Treble", "Player 1+ SOT", "virginbet",
                     5.5, 9.0, source="offer", db_path=db)
    settle_bet(bid, "lost", db_path=db)
    assert _pl(db, bid) == 0.0  # NOT -(9*(5.5-1)) lay liability


def test_bot_settle_free_bet_player_acca_loss_is_zero(tmp_path):
    """/settle path: 'Acca — England HT/2H + player SOT' free bet (seed id 100)."""
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "England vs Wales",
                     "Acca", "England HT/2H + player SOT", "virginbet",
                     6.0, 10.0, source="offer", db_path=db)
    app.handle_settle(f"/settle {bid} lost 5.0", db)  # close given for 'lost'
    assert _settled_pl(db, bid) == 0.0


def test_bot_settle_genuine_lay_loss_is_liability(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Lay (Bet Against)",
                     "A", "Betfair", 2.46, 60.0, source="punt", db_path=db)
    app.handle_settle(f"/settle {bid} lost 2.4", db)
    assert abs(_settled_pl(db, bid) - (-87.60)) < 1e-6


def test_cli_free_bet_player_acca_loss_is_zero(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "England vs Wales",
                     "Acca", "England HT/2H + player SOT", "virginbet",
                     6.0, 10.0, source="offer", db_path=db)
    settle = _load_settle()
    settle.main(["--db", db, "--bet-id", str(bid), "--outcome", "lost",
                 "--closing-odds", "5.0"])
    assert _settled_pl(db, bid) == 0.0


def test_cli_genuine_lay_loss_is_liability(tmp_path):
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Lay (Bet Against)",
                     "A", "Betfair", 2.46, 60.0, source="punt", db_path=db)
    settle = _load_settle()
    settle.main(["--db", db, "--bet-id", str(bid), "--outcome", "lost",
                 "--closing-odds", "2.4"])
    assert abs(_settled_pl(db, bid) - (-87.60)) < 1e-6


def test_cli_no_free_override_forces_stake_loss(tmp_path):
    """--no-free overrides a stored source='offer' back to normal-stake loss."""
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "A",
                     "bet365", 6.0, 10.0, source="offer", db_path=db)
    settle = _load_settle()
    settle.main(["--db", db, "--bet-id", str(bid), "--outcome", "lost",
                 "--closing-odds", "5.0", "--no-free"])
    assert _settled_pl(db, bid) == -10.0


def test_cli_free_override_forces_zero_loss(tmp_path):
    """--free overrides a stored source='model' into a free-bet (£0) loss."""
    db = str(tmp_path / "wca.db")
    bid = record_bet("2026-06-17T10:00:00", "M", "A vs B", "Match Odds", "A",
                     "bet365", 6.0, 10.0, source="model", db_path=db)
    settle = _load_settle()
    settle.main(["--db", db, "--bet-id", str(bid), "--outcome", "lost",
                 "--closing-odds", "5.0", "--free"])
    assert _settled_pl(db, bid) == 0.0


def test_three_settle_paths_agree_on_free_bet_player_loss(tmp_path):
    """The bot, CLI and store paths must book the identical £0 on the bug case."""
    db = str(tmp_path / "wca.db")
    common = ("2026-06-17T10:00:00", "M", "England vs Wales", "Treble",
              "Player 1+ SOT", "virginbet", 5.5, 9.0)
    a = record_bet(*common, source="offer", db_path=db)
    b = record_bet(*common, source="offer", db_path=db)
    c = record_bet(*common, source="offer", db_path=db)

    settle_bet(a, "lost", db_path=db)
    app.handle_settle(f"/settle {b} lost 5.0", db)
    _load_settle().main(["--db", db, "--bet-id", str(c), "--outcome", "lost",
                         "--closing-odds", "5.0"])

    assert _settled_pl(db, a) == _settled_pl(db, b) == _settled_pl(db, c) == 0.0
