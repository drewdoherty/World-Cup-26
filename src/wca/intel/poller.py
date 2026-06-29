"""Tiered, budget-aware polling planner for Market Intelligence collection.

WHY: OddsAPI credits are finite and most fixtures are far from kickoff, so a flat
"poll everything every N minutes" wastes the budget where it matters least. This
planner spends attention where price discovery actually happens — tightening
cadence and widening the market set as kickoff approaches — and degrades
gracefully when credits run low (shedding low-value markets, then slowing down,
but ALWAYS keeping moneyline).

It is a PURE function of its inputs: cadence decisions depend only on the
``now``, each fixture's kickoff, its ``last_polled_at``, the config, and the
current ``remaining_credits``. Nothing here touches the network or the clock —
``now``/``last_polled`` are injected so the planner is deterministic and fully
unit-testable. Only the CLI (``scripts/wca_intel_collect.py``) does IO.

Cadence table implemented (see ``data/intel_polling.yml`` for the editable copy):

    window to KO   cadence    markets
    -----------    -------    -------------------------------------------
    > 24h          6h         moneyline, totals(ou)
    24h - 3h       1h         moneyline, ou, ah, btts
    3h - 1h        30m        + player props where offered
    1h - KO        12m        full available set (+ team totals)

Budget governor: when ``remaining_credits <= floor_credits`` the planner sheds
markets by ascending priority (props -> AH -> ... ) while pinning moneyline;
when ``remaining_credits <= hard_floor_credits`` it ALSO doubles the effective
cadence (polls half as often). A fixture already past kickoff is never due.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

#: Market that is never dropped, regardless of budget pressure.
PINNED_MARKET = "moneyline"


@dataclass(frozen=True)
class Window:
    """One time-to-kickoff bucket: poll fixtures in ``[min, max)`` mins-to-KO
    no more often than ``cadence_s`` seconds, requesting ``markets``."""

    min_mins_to_ko: float
    max_mins_to_ko: Optional[float]   # None = unbounded (the far-out bucket)
    cadence_s: float
    markets: Tuple[str, ...]

    def contains(self, mins_to_ko: float) -> bool:
        if mins_to_ko < self.min_mins_to_ko:
            return False
        if self.max_mins_to_ko is not None and mins_to_ko >= self.max_mins_to_ko:
            return False
        return True


@dataclass(frozen=True)
class BudgetConfig:
    floor_credits: float = 500.0
    hard_floor_credits: float = 100.0
    regions: int = 1


@dataclass(frozen=True)
class PollingConfig:
    windows: Tuple[Window, ...]
    priority: Dict[str, int]
    budget: BudgetConfig

    def window_for(self, mins_to_ko: float) -> Optional[Window]:
        """Nearest matching window, or None if past kickoff (mins<0)."""
        if mins_to_ko < 0:
            return None
        for w in self.windows:           # ordered nearest-first
            if w.contains(mins_to_ko):
                return w
        return None

    def market_priority(self, market: str) -> int:
        if market == PINNED_MARKET:
            return 10_000               # effectively un-droppable
        return int(self.priority.get(market, 1))


# Baked-in defaults — kept in lock-step with data/intel_polling.yml so a missing
# or unparseable file still yields the documented behaviour.
_DEFAULT_WINDOWS: Tuple[Window, ...] = (
    Window(0,    60,   720.0,   ("moneyline", "ou", "ah", "btts", "player_prop", "team_total")),
    Window(60,   180,  1800.0,  ("moneyline", "ou", "ah", "btts", "player_prop")),
    Window(180,  1440, 3600.0,  ("moneyline", "ou", "ah", "btts")),
    Window(1440, None, 21600.0, ("moneyline", "ou")),
)
_DEFAULT_PRIORITY: Dict[str, int] = {
    "player_prop": 1, "team_total": 1, "ah": 2, "btts": 3, "ou": 4, "moneyline": 5,
}


def default_polling_config() -> PollingConfig:
    return PollingConfig(
        windows=_DEFAULT_WINDOWS,
        priority=dict(_DEFAULT_PRIORITY),
        budget=BudgetConfig(),
    )


def _windows_from_raw(raw_windows) -> Tuple[Window, ...]:
    ws: List[Window] = []
    for w in raw_windows:
        ws.append(Window(
            min_mins_to_ko=float(w.get("min_mins_to_ko", 0) or 0),
            max_mins_to_ko=(None if w.get("max_mins_to_ko") in (None, "null", "")
                            else float(w["max_mins_to_ko"])),
            cadence_s=float(w["cadence_s"]),
            markets=tuple(w.get("markets", ()) or ()),
        ))
    # nearest-first: smaller min_mins_to_ko buckets come first
    ws.sort(key=lambda x: x.min_mins_to_ko)
    return tuple(ws)


def load_polling_config(path: Optional[str] = None) -> PollingConfig:
    """Load the polling config from YAML, falling back to baked-in defaults.

    Uses PyYAML when importable; otherwise a tiny built-in parser handles the
    simple shape of ``data/intel_polling.yml``. Any missing file or parse error
    returns :func:`default_polling_config` — so tests never depend on the file.
    """
    if not path or not os.path.exists(path):
        return default_polling_config()
    try:
        raw = _read_yaml(path)
        if not isinstance(raw, dict):
            return default_polling_config()
        windows = _windows_from_raw(raw.get("windows") or [])
        if not windows:
            windows = _DEFAULT_WINDOWS
        priority = {str(k): int(v) for k, v in (raw.get("priority") or {}).items()}
        if not priority:
            priority = dict(_DEFAULT_PRIORITY)
        b = raw.get("budget") or {}
        budget = BudgetConfig(
            floor_credits=float(b.get("floor_credits", 500)),
            hard_floor_credits=float(b.get("hard_floor_credits", 100)),
            regions=int(b.get("regions", 1)),
        )
        return PollingConfig(windows=windows, priority=priority, budget=budget)
    except Exception:
        # Config is an optimisation, never load-bearing for correctness.
        return default_polling_config()


def _read_yaml(path: str):
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except ImportError:
        return _mini_yaml(path)


def _mini_yaml(path: str):
    """Minimal parser for the specific shape of intel_polling.yml.

    Handles top-level mappings, a ``windows:`` list of inline-list-bearing
    dicts, and ``priority:``/``budget:`` flat mappings. NOT a general YAML
    parser — only enough for this one config; anything unexpected raises and the
    caller falls back to defaults.
    """
    def scalar(tok: str):
        tok = tok.strip()
        if tok in ("null", "~", ""):
            return None
        if tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1].strip()
            return [s.strip() for s in inner.split(",")] if inner else []
        try:
            return int(tok)
        except ValueError:
            pass
        try:
            return float(tok)
        except ValueError:
            pass
        return tok

    root: Dict[str, object] = {}
    cur_section: Optional[str] = None
    windows: List[Dict[str, object]] = []
    cur_win: Optional[Dict[str, object]] = None
    flat: Optional[Dict[str, object]] = None

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            if indent == 0 and stripped.endswith(":"):
                cur_section = stripped[:-1].strip()
                if cur_section == "windows":
                    windows = []
                    root["windows"] = windows
                    cur_win = None
                    flat = None
                else:
                    flat = {}
                    root[cur_section] = flat
                continue
            if cur_section == "windows":
                if stripped.startswith("- "):
                    cur_win = {}
                    windows.append(cur_win)
                    stripped = stripped[2:].strip()
                if cur_win is not None and ":" in stripped:
                    k, _, v = stripped.partition(":")
                    cur_win[k.strip()] = scalar(v)
            elif flat is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                flat[k.strip()] = scalar(v)
    return root


# --------------------------------------------------------------------------- #
# Fixtures + plan
# --------------------------------------------------------------------------- #

@dataclass
class Fixture:
    """A fixture the planner reasons about. Provide ``ko_utc`` (ISO) OR an
    explicit ``mins_to_ko`` (used directly, overriding ko_utc)."""

    fixture_id: str
    ko_utc: Optional[str] = None
    mins_to_ko: Optional[float] = None


@dataclass
class FixturePlan:
    fixture_id: str
    due: bool
    markets: List[str]
    reason: str
    mins_to_ko: Optional[float] = None
    cadence_s: Optional[float] = None
    degraded: bool = False


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _mins_to_ko(fx: Fixture, now: datetime) -> Optional[float]:
    if fx.mins_to_ko is not None:
        return float(fx.mins_to_ko)
    ko = _parse_iso(fx.ko_utc)
    if ko is None:
        return None
    return (ko - now).total_seconds() / 60.0


def _budget_factor(remaining: Optional[float], budget: BudgetConfig) -> Tuple[int, float, str]:
    """Return (min_priority_kept, cadence_multiplier, reason_suffix).

    * remaining unknown / above floor   -> keep all (0), 1x cadence.
    * floor >= remaining > hard_floor    -> shed priority<2 (keep moneyline + mid
      tiers), 1x cadence.
    * remaining <= hard_floor            -> keep only highest non-pinned tier
      present is irrelevant; shed all but moneyline + top tier, 2x cadence.
    """
    if remaining is None or remaining > budget.floor_credits:
        return 0, 1.0, ""
    if remaining > budget.hard_floor_credits:
        return 2, 1.0, " budget<=floor: shed low-priority markets"
    return 4, 2.0, " budget<=hard_floor: shed markets + halve cadence"


def _shed_markets(markets: Sequence[str], min_priority: int,
                  cfg: PollingConfig) -> List[str]:
    """Drop markets below ``min_priority``; moneyline is always kept."""
    kept = [m for m in markets
            if m == PINNED_MARKET or cfg.market_priority(m) >= min_priority]
    if PINNED_MARKET not in kept and PINNED_MARKET in markets:
        kept.insert(0, PINNED_MARKET)
    return kept


def plan_polls(
    fixtures: Sequence[Fixture],
    *,
    now: datetime,
    last_polled_at: Optional[Dict[str, Optional[datetime]]] = None,
    config: Optional[PollingConfig] = None,
    remaining_credits: Optional[float] = None,
    available_markets: Optional[Sequence[str]] = None,
) -> List[FixturePlan]:
    """Decide, per fixture, whether a poll is due now and which markets to pull.

    Parameters
    ----------
    fixtures
        Fixtures to consider.
    now
        The reference instant (injected; never read the clock here).
    last_polled_at
        Map fixture_id -> last poll datetime (tz-aware) or None if never polled.
    config
        A :class:`PollingConfig`; defaults to :func:`default_polling_config`.
    remaining_credits
        Current OddsAPI credit balance, or None if unknown (no degradation).
    available_markets
        If given, intersect each window's market list with this set (what the
        wired sources actually offer this run) so we never plan an unavailable
        market. ``moneyline`` is still pinned even if missing here.

    Returns one :class:`FixturePlan` per fixture (including not-due ones, with
    ``due=False`` and a reason) so the caller can log/inspect every decision.
    """
    cfg = config or default_polling_config()
    last = last_polled_at or {}
    avail = set(available_markets) if available_markets is not None else None
    min_prio, cad_mult, budget_note = _budget_factor(remaining_credits, cfg.budget)

    plans: List[FixturePlan] = []
    for fx in fixtures:
        mtk = _mins_to_ko(fx, now)
        if mtk is None:
            plans.append(FixturePlan(fx.fixture_id, False, [], "no kickoff time", None))
            continue
        win = cfg.window_for(mtk)
        if win is None:
            plans.append(FixturePlan(fx.fixture_id, False, [], "past kickoff", mtk))
            continue

        markets = list(win.markets)
        if avail is not None:
            markets = [m for m in markets if m in avail or m == PINNED_MARKET]
        degraded = min_prio > 0
        if degraded:
            markets = _shed_markets(markets, min_prio, cfg)

        effective_cadence = win.cadence_s * cad_mult
        lp = last.get(fx.fixture_id)
        if lp is None:
            due, reason = True, "never polled"
        else:
            elapsed = (now - lp).total_seconds()
            if elapsed >= effective_cadence:
                due = True
                reason = "due: %.0fs >= cadence %.0fs" % (elapsed, effective_cadence)
            else:
                due = False
                reason = "not due: %.0fs < cadence %.0fs" % (elapsed, effective_cadence)
        reason += budget_note

        plans.append(FixturePlan(
            fixture_id=fx.fixture_id,
            due=due,
            markets=markets if due else [],
            reason=reason,
            mins_to_ko=round(mtk, 1),
            cadence_s=effective_cadence,
            degraded=degraded,
        ))
    return plans
