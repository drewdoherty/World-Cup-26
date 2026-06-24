"""Event-driven cash-out watcher (the loop brain, testable in isolation).

:class:`CashoutWatcher.tick` takes a snapshot of held positions + live scores and
decides, per position, whether to do nothing, wait out the VAR cooldown, log a
shadow sell, or (when armed) execute a real cash-out SELL. All I/O — fetching the
book and executing the order — is injected as callables so the decision logic is
unit-testable with fakes and no network.

Safety rails baked in (these hold even in armed auto-sell mode):

* **VAR cooldown** — a kill must persist for ``var_cooldown_s`` of wall-clock
  (across ≥1 re-observation) before we sell, so a goal that VAR chalks off in the
  first seconds doesn't trigger a dump. A score that ticks back down (reversal)
  cancels the pending sell.
* **Dedup / claim** — :mod:`wca.pm.cashout_state` records a claim before the
  order, so the same dead position is never sold twice across ticks or restarts.
* **Min-proceeds floor** — a sell whose realistic proceeds round to ~nothing is
  skipped (pure downside given VAR risk + gas).
* **Shadow by default** — ``arm=False`` never executes; it only logs what it
  *would* do, for measuring the edge before trusting real money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from wca.pm import cashout, cashout_state
from wca.pm.cashout import classify_market

KILLABLE = cashout.KILLABLE_KINDS


@dataclass
class WatchConfig:
    min_proceeds: float = 1.0          # don't sell for ~nothing (safety rail)
    price_floor: float = 0.0           # don't sell into bids below this (0 = any)
    var_cooldown_s: float = 45.0       # kill must persist this long before selling
    undercut_ticks: int = 0
    tick: float = 0.01
    kinds: tuple = KILLABLE             # which market kinds to arm
    arm: bool = False                  # False => shadow (never execute)


@dataclass
class CashoutWatcher:
    cfg: WatchConfig
    state_db: str
    # In-memory suppression of DRY-ARM re-fires within a single run (so dry-arm
    # signs each killed position once per process, not every tick). Resets on
    # restart — harmless, since dry-arm never moves money or persistent state.
    _dry_shown: set = field(default_factory=set)

    def tick(
        self,
        positions: List[Any],
        scores_events: List[Dict[str, Any]],
        *,
        now: float,
        book_fn: Callable[[str], Optional[Dict[str, Any]]],
        execute_fn: Callable[[Dict[str, Any]], Any],
    ) -> List[Dict[str, Any]]:
        """Process one poll. ``now`` is a wall-clock epoch (seconds).

        ``execute_fn(proposal)`` must return a structured result dict
        ``{submitted, settled, dry_run, error, message, ...}`` (see
        ``wca.bot.app.execute_cashout``). The watcher advances the persistent
        claim ONLY in step with what execution actually did — it marks a token
        ``sold`` only when ``settled`` is True. Returns action records for logging.
        """
        actions: List[Dict[str, Any]] = []

        for pos in positions:
            if not getattr(pos, "is_open", True):
                continue
            if classify_market(pos.title, pos.outcome) not in self.cfg.kinds:
                continue
            asset = pos.asset

            # Cheap kill test first (no book fetch).
            d = cashout.decide_cashout(
                pos, scores_events, None,
                min_proceeds=self.cfg.min_proceeds, price_floor=self.cfg.price_floor,
            )

            if d.action != "sell":
                # Not killed / unmappable. A score that is no longer a kill is a
                # VAR reversal: cancel any pending (not-yet-sold) cash-out.
                if d.action == "not_killed":
                    phase = cashout_state.get_phase(asset, self.state_db)
                    if phase in ("observed", "claimed"):
                        cashout_state.clear(asset, self.state_db)
                        self._dry_shown.discard(asset)
                        actions.append({"asset": asset, "action": "reversal",
                                        "title": pos.title, "reason": d.reason})
                elif d.action == "no_match":
                    actions.append({"asset": asset, "action": "no_match",
                                    "title": pos.title, "reason": d.reason})
                continue

            # --- killed ---
            if cashout_state.is_handled(asset, self.state_db):
                continue  # claimed / sold / settle_failed — don't start a new sell
            if asset in self._dry_shown:
                continue  # dry-arm already exercised this position this run

            # Persistent VAR cooldown: measured from the first sighting, so a
            # restart mid-cooldown does NOT reset the clock.
            first = cashout_state.observe(
                asset, getattr(pos, "event_slug", "") or "", now,
                detail=(pos.title or "")[:80], db_path=self.state_db,
            )
            elapsed = now - first
            if elapsed < self.cfg.var_cooldown_s:
                actions.append({"asset": asset, "action": "cooldown", "title": pos.title,
                                "reason": d.reason, "elapsed_s": round(elapsed, 1),
                                "remaining_s": round(self.cfg.var_cooldown_s - elapsed, 1)})
                continue

            # Cooldown passed — price the exit against the live book.
            try:
                book = book_fn(asset)
            except Exception as exc:  # noqa: BLE001 — a book read must not kill the loop
                actions.append({"asset": asset, "action": "error",
                                "title": pos.title, "error": "book: %s" % exc})
                continue

            d2 = cashout.decide_cashout(
                pos, scores_events, book,
                min_proceeds=self.cfg.min_proceeds, price_floor=self.cfg.price_floor,
                undercut_ticks=self.cfg.undercut_ticks, tick=self.cfg.tick,
            )
            if d2.action != "sell" or not d2.proposal:
                actions.append({"asset": asset, "action": d2.action,
                                "title": pos.title, "reason": d2.reason})
                continue
            proposal = d2.proposal

            if not self.cfg.arm:
                actions.append({"asset": asset, "action": "shadow_sell",
                                "title": pos.title, "proposal": proposal,
                                "reason": d2.reason})
                continue

            # ARMED: claim (atomic dedup) then execute.
            if not cashout_state.claim(
                asset, proposal.get("match_id") or "",
                detail=proposal.get("cashout_reason", ""), db_path=self.state_db,
            ):
                continue  # lost the race
            try:
                result = execute_fn(proposal)
            except Exception as exc:  # noqa: BLE001
                # Unexpected throw from the executor: revert to observed so a later
                # tick can retry, and surface loudly.
                cashout_state.revert_to_observed(asset, detail="execute threw",
                                                 db_path=self.state_db)
                actions.append({"asset": asset, "action": "error", "title": pos.title,
                                "error": "execute: %s" % exc, "proposal": proposal})
                continue

            actions.append(self._apply_result(asset, pos, proposal, result, d2.reason))

        return actions

    def _apply_result(self, asset, pos, proposal, result, reason) -> Dict[str, Any]:
        """Advance the persistent claim to match what execution ACTUALLY did,
        keyed on the executor's explicit ``outcome`` (see app.execute_cashout)."""
        rec = {"asset": asset, "title": pos.title, "proposal": proposal,
               "result": result, "reason": reason}
        r = result if isinstance(result, dict) else {}
        # Back-compat: derive an outcome if a bare result was returned.
        outcome = r.get("outcome") or (
            "sold" if r.get("settled") else
            "unconfirmed" if r.get("submitted") else
            "dry_run" if r.get("dry_run") else "no_fill"
        )

        if outcome == "sold":
            cashout_state.mark_sold(asset, detail=str(r.get("message"))[:160],
                                    db_path=self.state_db)
            rec["action"] = "sold"
        elif outcome in ("unconfirmed", "settle_failed"):
            # Live order went out but the fill/booking couldn't be confirmed: do
            # NOT auto-retry — flag for manual reconciliation.
            cashout_state.set_phase(asset, "settle_failed",
                                    detail=str(r.get("error") or r.get("message"))[:160],
                                    db_path=self.state_db)
            rec["action"] = "settle_failed"
            rec["error"] = r.get("error") or r.get("message")
        elif outcome == "dry_run":
            # DRY-ARM: signed, not submitted. Don't lock the position (a real LIVE
            # run must still be able to sell it); just suppress re-firing this run.
            cashout_state.revert_to_observed(asset, detail="dry-arm", db_path=self.state_db)
            self._dry_shown.add(asset)
            rec["action"] = "dry_arm"
        elif outcome == "no_fill":
            # FOK didn't fill (book moved). Keep the cooldown; retry next tick.
            cashout_state.revert_to_observed(asset, detail="no-fill", db_path=self.state_db)
            rec["action"] = "no_fill"
        else:
            # place_failed / no_trader / unknown: retry next tick, surface loudly.
            cashout_state.revert_to_observed(asset, detail=str(outcome), db_path=self.state_db)
            rec["action"] = "error"
            rec["error"] = r.get("error") or r.get("message")
        return rec
