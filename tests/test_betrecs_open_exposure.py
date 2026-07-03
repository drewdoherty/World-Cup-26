"""Tests for the live open-exposure block in the Action Desk feed (F6).

Guards that ``scripts/wca_betrecs.py`` derives ``meta.open_exposure.n_open`` from
the LIVE ledger (``bets WHERE status='open'``) rather than from a possibly-stale
shipped exposure feed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_betrecs as br  # noqa: E402
from wca.ledger import store  # noqa: E402


def _db_with_n_open(tmp_path, n_open: int):
    db = str(tmp_path / "ledger.db")
    store.init_db(db)
    conn = store._connect(db)
    for i in range(n_open):
        conn.execute(
            "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
            "platform, decimal_odds, stake, status, source, account) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-06-13T10:00:00+00:00", f"m{i}", "X vs Y", "Full-time result",
             "X", "betfair_sportsbook", 2.0, 10.0, "open", "model", "1"),
        )
    # one settled bet to prove the filter excludes non-open rows
    conn.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, status, source, account) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-06-13T10:00:00+00:00", "settled", "A vs B", "Full-time result",
         "A", "betfair_sportsbook", 2.0, 10.0, "won", "model", "1"),
    )
    conn.commit()
    conn.close()
    return db


def test_ledger_open_count_filters_status(tmp_path):
    db = _db_with_n_open(tmp_path, 8)
    assert br._ledger_open_count(db) == 8


def test_ledger_open_count_missing_db_is_none(tmp_path):
    assert br._ledger_open_count(str(tmp_path / "nope.db")) is None


def test_open_exposure_n_open_from_ledger_ignores_stale_feed(tmp_path):
    """A stale feed claiming 59 must not override the live ledger's 8."""
    db = _db_with_n_open(tmp_path, 8)
    stale_feed = {"metrics": {"n_open_bets": 59, "ev": 9.9, "worst_case": -1096.22,
                              "p_profit": 0.6}, "n_open_bets": 59}
    block = br._open_exposure(db, stale_feed)
    assert block["n_open"] == 8
    assert block["source"] == "ledger"
    # The stale feed's fabricated worst_case must not leak through.
    assert block["worst_case"] != -1096.22
    assert block["p_profit"] != 0.6


def test_open_exposure_empty_ledger(tmp_path):
    db = _db_with_n_open(tmp_path, 0)
    block = br._open_exposure(db, {"metrics": {"n_open_bets": 59}})
    assert block["n_open"] == 0
    assert block["source"] == "ledger"


def test_open_exposure_falls_back_to_feed_when_db_absent(tmp_path):
    """No runtime DB → feed used, but the fallback is flagged (not silent)."""
    feed = {"metrics": {"n_open_bets": 4, "ev": 1.2}}
    block = br._open_exposure(str(tmp_path / "missing.db"), feed)
    assert block["n_open"] == 4
    assert "feed" in block["source"]


def test_shipped_bet_recs_open_exposure_is_structurally_sound():
    """Regression guard on the committed feed — structure + provenance only.

    The shipped feed is rewritten by BOTH the mini publish job (live ledger →
    real count) and CI daily-card (no ledger → 0), so the exact open-bet count
    is committer-dependent. The old ``assert n_open == 8`` pin kept the suite
    red on every data commit (0 != 8, 77 != 8). The derivation logic is fully
    covered by the fixture tests above; here we keep only the stale-59 guard
    and structural checks.
    """
    shipped = json.loads((_REPO / "site" / "bet_recs.json").read_text(encoding="utf-8"))
    block = shipped["meta"]["open_exposure"]
    n_open = block["n_open"]
    assert isinstance(n_open, int) and n_open >= 0
    assert n_open != 59, "bet_recs.json regressed to the stale 59-open-bet count"
    # Provenance (ledger vs feed fallback) must stay disclosed in the feed.
    assert "source" in block


def test_main_end_to_end_with_default_args(tmp_path, monkeypatch):
    """Regression: main() must run with DEFAULT args (pm_bankroll=None resolved
    from the rule) and absent feeds — the 2026-07-03 publish outage was a
    %-format on args.pm_bankroll=None that no test executed."""
    out = tmp_path / "recs.json"
    monkeypatch.chdir(tmp_path)  # feeds/db all absent -> tolerant paths
    rc = br.main_with_args([
        "--db", str(tmp_path / "missing.db"),
        "--out", str(out),
    ]) if hasattr(br, "main_with_args") else None
    if rc is None:
        import sys
        monkeypatch.setattr(sys, "argv", ["wca_betrecs.py",
                                          "--db", str(tmp_path / "missing.db"),
                                          "--out", str(out)])
        rc = br.main()
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["meta"]["pm_pool"]["bankroll"] > 0
    assert "advancement_futures" in payload["meta"]["coverage"]
