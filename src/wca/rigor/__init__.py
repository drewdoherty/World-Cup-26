"""Module D — the repeatable-edge VERDICT battery.

This package answers one question as conservatively as the data allows: *do we
have statistical evidence of a repeatable betting edge, or not yet?*  Its
**default answer is INSUFFICIENT SAMPLE**.  At the current sample size (72-ish
settled money bets, 25 with a captured close, single-digit settled accas, a
backfilled paper book of ~23 settled fixtures) that default is almost always
the correct one, and the battery is engineered to *say so* rather than to
manufacture a green light.

Design principles (shared by every gate)
----------------------------------------
* **Wilson intervals, never bare points.**  A rate is reported as
  ``[lo, hi]`` at 95%; a point estimate alone is never used for a decision.
* **CLV is NULL where no close exists** — pushes / voids are excluded from
  *both* numerator and denominator of every rate, and a missing close is
  ``None`` (never silently ``0``).
* **Fair-vs-fair only for CLV.**  Both sides are de-vigged; we never compare a
  model price to a raw vigged book price.
* **n_eff, not n.**  Bets cluster: many legs share one fixture, an acca is one
  bet not N, and the same fixture is re-predicted across many builds.  The
  effective sample is a cluster-bootstrap-implied count (cluster = ``match_id``
  for singles, ``acca_id`` for accas).  Futures are permanently ``N ~= 1`` and
  therefore permanently insufficient.
* **Outcome-anchored greens only.**  CLV alone can never turn the verdict
  green — a best-price-only artifact (positive CLV, zero predictive skill) must
  *not* green.  Green requires a settled-outcome gate (skill or calibration) to
  also pass, plus stability.

Public surface
--------------
``build_rigor(...)`` (re-exported from :mod:`wca.rigor.build`) assembles the
whole ``rigor.json`` payload from a read-only ``wca.db`` path, the
``model_predictions_log.jsonl`` + results join, and an optional ``dev.db``
prediction ledger.
"""

from __future__ import annotations

from wca.rigor.build import build_rigor, RigorInputs  # noqa: F401

__all__ = ["build_rigor", "RigorInputs"]
