"""Polymarket advancement close-capture + CLV-stamping shared logic.

Two-sided flow (see module docstrings in the two CLI scripts for the exact
commands a human runs):

1. **MacBook side** (``scripts/wca_pm_close_capture.py``) — Polymarket's CLOB
   is reachable only from the MacBook (VPN); the Mac mini (production, the
   canonical ``data/wca.db`` ledger) is PM-blind.  The MacBook script pulls
   :mod:`wca.data.pm_clob_history` top-of-book / price-history for each
   advancement/moneyline token, resolves the "close" as of each team's
   deciding-match kickoff, and appends the result to a small committed JSON
   artifact, ``data/pm_closes.json``.
2. **Mini side** (``scripts/wca_pm_stamp_clv.py``) — reads that artifact
   (delivered by git autopull, no network call) and stamps
   ``closing_odds``/``clv`` onto matching ``platform='polymarket'`` ledger
   bets, the same ``backed / close - 1`` convention used by
   :mod:`wca.closecapture`.

This module holds the logic shared by both sides: the artifact's row schema,
idempotent read/append, the deciding-match-kickoff lookup (from
``data/processed/wc2026_results.json``), and the bet-matching join used by the
stamper. Kept separate from the two CLI scripts so both can be unit-tested
without touching the network or a real ledger.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from wca.data import teamnames

# ---------------------------------------------------------------------------
# Artifact schema + idempotent read/append.
# ---------------------------------------------------------------------------

#: Fields carried by every row of ``data/pm_closes.json``.
CLOSE_FIELDS = (
    "condition_id",
    "token_id",
    "question",
    "close_ts_utc",
    "mid",
    "best_bid",
    "best_ask",
    "source",
    "captured_utc",
)

_DEFAULT_ARTIFACT = "data/pm_closes.json"


def _close_key(row: Dict[str, Any]) -> Tuple[Any, Any]:
    """Identity of one close event: (token, close timestamp).

    Deliberately *not* keyed on ``captured_utc`` so a rerun that recomputes
    the same close (e.g. a backfill re-run) is recognised as the same row
    rather than appended a second time.
    """
    return (row.get("token_id"), row.get("close_ts_utc"))


def load_closes(artifact_path: str = _DEFAULT_ARTIFACT) -> List[Dict[str, Any]]:
    """Return the rows already in *artifact_path* (``[]`` if absent/empty/bad)."""
    if not os.path.exists(artifact_path):
        return []
    try:
        with open(artifact_path, "r") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("closes", [])
    return list(data) if isinstance(data, list) else []


def append_closes(
    new_rows: List[Dict[str, Any]],
    artifact_path: str = _DEFAULT_ARTIFACT,
) -> Tuple[List[Dict[str, Any]], int]:
    """Idempotently merge *new_rows* into *artifact_path*.

    One row per ``(token_id, close_ts_utc)`` — a row already present (by that
    key) is left untouched (first-write-wins; a close price does not change
    after the fact). Returns ``(all_rows, n_added)`` and writes the file
    (sorted by ``close_ts_utc`` then ``token_id`` for a stable diff) whenever
    at least one row was actually new; a no-op call touches nothing on disk.
    """
    existing = load_closes(artifact_path)
    seen = {_close_key(r) for r in existing}
    added = []
    for row in new_rows:
        key = _close_key(row)
        if key in seen:
            continue
        seen.add(key)
        added.append({field: row.get(field) for field in CLOSE_FIELDS})

    if not added:
        return existing, 0

    merged = existing + added
    merged.sort(key=lambda r: (str(r.get("close_ts_utc") or ""), str(r.get("token_id") or "")))

    out_dir = os.path.dirname(os.path.abspath(artifact_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = artifact_path + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(merged, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp_path, artifact_path)
    return merged, len(added)


# ---------------------------------------------------------------------------
# Deciding-match kickoff resolution (from data/processed/wc2026_results.json).
# ---------------------------------------------------------------------------

_DEFAULT_RESULTS = "data/processed/wc2026_results.json"


def _canon(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return (teamnames.canonical(name) or "").strip().casefold()


def load_team_last_kickoff(
    results_path: str = _DEFAULT_RESULTS,
) -> Dict[str, str]:
    """Map canonical team name -> kickoff (UTC ISO) of its LATEST played match.

    ``data/processed/wc2026_results.json`` is one row per played fixture
    (``fixture`` = ``"Home vs Away"``, ``kickoff_utc`` when known). A team's
    most recent played match is used as the "deciding match" for whichever
    advancement round it has just settled (its last group match decides
    R32 advancement; its last knockout tie decides the next round) — a
    reasonable single-kickoff proxy for a stage that, unlike a 1X2 market,
    has no single deciding fixture in the API. Rows with no ``kickoff_utc``
    are skipped (date-only fallback is not precise enough for a close).
    """
    if not os.path.exists(results_path):
        return {}
    try:
        with open(results_path, "r") as fh:
            payload = json.load(fh)
    except (ValueError, OSError):
        return {}
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}

    latest: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = row.get("fixture")
        kickoff = row.get("kickoff_utc")
        if not fixture or not kickoff:
            continue
        parts = str(fixture).split(" vs ")
        if len(parts) != 2:
            continue
        for raw_team in parts:
            team_c = _canon(raw_team)
            if not team_c:
                continue
            prev = latest.get(team_c)
            if prev is None or str(kickoff) > prev:
                latest[team_c] = str(kickoff)
    return latest


# ---------------------------------------------------------------------------
# Market question -> team parsing (pm_markets.question / .team already gives
# this on the MacBook side; the mini-side stamper re-derives it from a bet's
# free-text match_desc/selection since token_id is not yet populated on the
# ledger — see docstring in scripts/wca_pm_stamp_clv.py for the join order).
# ---------------------------------------------------------------------------

# "Will <Team> reach the Round of 16 at the 2026 FIFA World Cup?" etc.
_QUESTION_RE = re.compile(
    r"^Will\s+(?P<team>.+?)\s+(?:reach|win|advance|be eliminated)",
    re.IGNORECASE,
)


def team_from_question(question: Any) -> Optional[str]:
    """Best-effort canonical team name parsed from a PM market question."""
    if not isinstance(question, str):
        return None
    match = _QUESTION_RE.match(question.strip())
    if not match:
        return None
    team = match.group("team").strip()
    canon = teamnames.canonical(team)
    return canon or None


# "Round of 16" / "Round of 32" / "Quarterfinals" / "Semifinals" / "Final" ->
# the advancement.STAGE_ORDER short code, kept local (no import of
# wca.advancement — that module drags in the sim stack, more than this needs).
_STAGE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"round of 32|knockout stage|\br32\b", "R32"),
    (r"round of 16|\br16\b", "R16"),
    (r"quarterfinal|\bqf\b", "QF"),
    (r"semifinal|\bsf\b", "SF"),
    # "Win Group C" must be checked BEFORE the bare "final" pattern
    # (group_winner questions never contain "final", but ordering this first
    # keeps the two win-type patterns adjacent/readable) and before the
    # tournament-winner pattern, since both start with "win".
    (r"win group\s+\w+\b", "GW"),
    (r"\bfinal\b", "F"),
    (r"\bwin the (?:2026 )?(?:fifa )?world cup\b|champion", "win"),
)


def stage_from_question(question: Any) -> Optional[str]:
    """Best-effort advancement-stage code (R32/R16/QF/GW/SF/F/win) from a question.

    ``GW`` = "win the GROUP" (distinct from ``win`` = win the tournament) —
    both a ``group_winner`` and a ``winner`` PM market can exist for the same
    team, so keeping them separate stage codes is what lets
    :func:`index_closes` tell them apart.
    """
    if not isinstance(question, str):
        return None
    lowered = question.casefold()
    for pattern, stage in _STAGE_PATTERNS:
        if re.search(pattern, lowered):
            return stage
    return None


# ---------------------------------------------------------------------------
# CLV arithmetic — same convention as wca.closecapture: backed / close - 1.
# ---------------------------------------------------------------------------


def clv_from_mid(decimal_odds: float, close_mid: float) -> Optional[float]:
    """CLV for a bet backed at *decimal_odds* against a PM close *mid* in [0,1].

    The close mid is a YES probability; its fair decimal price is ``1/mid``.
    Returns ``None`` when the close is degenerate (``mid`` not in ``(0, 1)``)
    or *decimal_odds* is not a usable positive number.
    """
    try:
        mid = float(close_mid)
        backed = float(decimal_odds)
    except (TypeError, ValueError):
        return None
    if not (0.0 < mid < 1.0) or backed <= 0:
        return None
    closing_odds = 1.0 / mid
    return backed / closing_odds - 1.0


def closing_odds_from_mid(close_mid: Any) -> Optional[float]:
    """Fair decimal closing price for a PM YES mid in ``(0, 1)``, else ``None``."""
    try:
        mid = float(close_mid)
    except (TypeError, ValueError):
        return None
    if not 0.0 < mid < 1.0:
        return None
    return 1.0 / mid


# ---------------------------------------------------------------------------
# Bet <-> close-row join (mini-side stamper).
# ---------------------------------------------------------------------------


# Leading "No — Ghana not eliminated in Round of 32" / "Yes — Japan reaches
# R16" form, seen on manually-recorded advancement bets (see the ledger's
# ``selection`` column, e.g. bet id 57 in the local dev copy of wca.db).
_LEADING_YESNO_RE = re.compile(r"^(?:yes|no)\s*[—\-:]\s*", re.IGNORECASE)


def _selection_team(selection: Any) -> Optional[str]:
    """Canonical team parsed off a PM advancement selection string.

    Handles ``"<Team> Yes"``, ``"<Team> No"``, ``"<Team> reach R16 - No"``,
    and ``"No — <Team> not eliminated in Round of 32"``-style strings recorded
    by different ingestion paths (see the ``pm_markets`` sample rows in
    ``data/pm_orderflow.db`` and the ledger's own ``selection`` column) by
    stripping the Yes/No token (leading or trailing) and any parenthetical/
    dash/stage-clause suffix, then canonicalising what remains.
    """
    if not isinstance(selection, str):
        return None
    sel = selection.strip()
    # Drop a leading "Yes — " / "No — " prefix.
    sel = _LEADING_YESNO_RE.sub("", sel).strip()
    # Drop a trailing " - No" / " - Yes" or " No" / " Yes".
    sel = re.sub(r"\s*-\s*(yes|no)\s*$", "", sel, flags=re.IGNORECASE)
    sel = re.sub(r"\s+(yes|no)\s*$", "", sel, flags=re.IGNORECASE)
    # Drop a leading/trailing negation clause some ingesters embed
    # ("<Team> not eliminated in Round of 32", "<Team> reach R16").
    sel = re.sub(
        r"\s+(not eliminated|eliminated|reach(?:es)?|advance(?:s)? to|win(?:s)?)\b.*$",
        "",
        sel,
        flags=re.IGNORECASE,
    ).strip()
    return teamnames.canonical(sel) or None


def index_closes(
    closes: List[Dict[str, Any]],
) -> Tuple[Dict[Tuple[str, Optional[str]], Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Build the ``(team_c, stage) -> row`` and ``team_c -> [rows]`` indices.

    Team + stage come from each row's ``question`` text (the market's own
    wording, captured verbatim by the MacBook side) via
    :func:`team_from_question` / :func:`stage_from_question`. Rows whose
    question doesn't parse to a team are dropped from both indices (never
    silently mismatched to the wrong team).  When two rows resolve to the
    same ``(team, stage)`` key the LATEST ``close_ts_utc`` wins (a rerun that
    captured a later, more accurate close for the same deciding match).
    """
    by_team_stage: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    by_team: Dict[str, List[Dict[str, Any]]] = {}
    for row in closes:
        team = team_from_question(row.get("question"))
        if not team:
            continue
        team_c = _canon(team)
        stage = stage_from_question(row.get("question"))
        key = (team_c, stage)
        existing = by_team_stage.get(key)
        if existing is None or str(row.get("close_ts_utc") or "") > str(
            existing.get("close_ts_utc") or ""
        ):
            by_team_stage[key] = row
        by_team.setdefault(team_c, []).append(row)
    return by_team_stage, by_team


def _is_no_selection(selection: Any) -> bool:
    """True when a selection backs the "No" (complement) share.

    Handles both the trailing (``"Mexico No"``) and leading (``"No — Ghana
    not eliminated..."``) forms real ledger rows use — see
    :func:`_selection_team`'s docstring for the same two shapes.
    """
    if not isinstance(selection, str):
        return False
    sel = selection.strip()
    if re.match(r"^no\s*[—\-:]", sel, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\s(no)\s*$", sel, flags=re.IGNORECASE))


def match_bet_to_close(
    match_desc: Any,
    selection: Any,
    notes: Any,
    closes_by_team_stage: Dict[Tuple[str, Optional[str]], Dict[str, Any]],
    closes_by_team: Dict[str, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Resolve a ledger bet to a captured close row, else ``None``.

    Join order (documented fuzziness — no ``token_id`` is populated on
    historical bet rows, see the stamper script docstring):

    1. **team + stage**, both parsed from ``match_desc``/``selection``/
       ``notes`` — exact, when the row's ``question`` stage can be told apart
       (the common case: ``"Will Ghana be eliminated in the Round of 32..."``).
    2. **team only**, when the stage can't be parsed but the team has exactly
       ONE captured close (unambiguous fallback) — a team with multiple
       captured stage-closes is left unstamped rather than guessed at.

    The returned row is unchanged by Yes/No — :func:`fair_close_mid_for_bet`
    is what applies the ``1 - mid`` complement for a "No" selection; keeping
    that adjustment out of the join keeps this function a pure lookup.
    """
    team = _selection_team(selection) or _selection_team(match_desc)
    if not team:
        return None
    team_c = _canon(team)

    stage = stage_from_question(match_desc) or stage_from_question(notes)
    if stage is not None:
        row = closes_by_team_stage.get((team_c, stage))
        if row is not None:
            return row

    candidates = closes_by_team.get(team_c) or []
    if len(candidates) == 1:
        return candidates[0]
    return None


def fair_close_mid_for_bet(selection: Any, row: Dict[str, Any]) -> Optional[float]:
    """The close row's YES mid, complemented to ``1 - mid`` for a "No" bet.

    A PM advancement bet can back either share of the same market (e.g. bet
    id 57 in the ledger, ``"No — Ghana not eliminated in Round of 32"``,
    backs Ghana's NO/eliminated share) — its fair closing PROBABILITY is
    ``1 - yes_mid``, not the raw captured YES mid, or the CLV sign comes out
    backwards. Returns ``None`` when the row carries no usable ``mid``.
    """
    try:
        mid = float(row.get("mid"))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= mid <= 1.0:
        return None
    return (1.0 - mid) if _is_no_selection(selection) else mid


# Re-exported under the plain name other modules expect.
canon = _canon
