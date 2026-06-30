#!/usr/bin/env python3
"""Recalibrate the serialized Dixon-Coles total-goals level anchor.

One-off regeneration utility for ``data/dc_params_corrected.json``. The raw
penalised-MLE intercept ``mu`` is fit over a 49k-match international corpus
dominated by lower-scoring defensive matches and implies a World-Cup slate total
of ~2.34 goals/match -- ~0.4-0.5 below the recent World-Cup base rate and ~0.66
below the realized WC2026 rate (significant by paired t, p~=0.049; see
docs/research/wca_alpha_2026/08_xg_and_totals.md).

This applies the SAME scalar ``mu`` shift that
``card.fit_models(dc_level_target=2.81)`` applies at fit time, so the serialized
params consumed by ``scripts/wca_clv_by_bet.py`` /
``scripts/wca_recompute_open_bets.py`` /
``scripts/microstructure/synthetic_pricing.py`` stay consistent with the live
card. ``attack``/``defence``/``rho``/``home_advantage`` are untouched, so the
supremacy log-ratio -- and the raw 1X2 difference -- is invariant; only the
overall goal level moves.

The reference slate is the played WC2026 fixtures (neutral venues), matching the
reproduction in section 2 of the research note. Re-running is idempotent against
a given target only if started from the un-shifted params; it stamps the applied
shift into the file so a second run is a no-op-by-detection.

Usage::

    python scripts/wca_recalibrate_dc_level.py            # target 2.81
    python scripts/wca_recalibrate_dc_level.py --target 2.70
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from wca.models.dixon_coles import DixonColesModel  # noqa: E402
# Shared WC-slate reference — single source of truth with ``card.fit_models`` and
# ``scripts/wca_recompute_open_bets.py``, so the anchor slate cannot drift.
from wca.card import _wc_level_reference_fixtures as _reference_fixtures  # noqa: E402

DC_JSON = os.path.join(ROOT, "data", "dc_params_corrected.json")
DEFAULT_TARGET = 2.81


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET,
                    help="target mean total goals/match on the WC2026 slate")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the shift without writing the file")
    args = ap.parse_args()

    raw = json.load(open(DC_JSON), object_pairs_hook=collections.OrderedDict)
    if "level_target" in raw:
        print("ERROR: file already carries level_target=%s (mu=%.5f); "
              "start from the un-shifted params to re-target."
              % (raw.get("level_target"), raw.get("mu")), file=sys.stderr)
        sys.exit(2)

    old_mu = float(raw["mu"])
    dc = DixonColesModel.from_dict(raw)
    fixtures = _reference_fixtures(dc)
    if not fixtures:
        print("ERROR: no matched WC2026 reference fixtures", file=sys.stderr)
        sys.exit(1)

    pre = st.mean(sum(dc.expected_lambdas(h, a, neutral=True, warn=False))
                  for h, a in fixtures)
    delta = dc.recalibrate_level(args.target, neutral=True, fixtures=fixtures)
    post = st.mean(sum(dc.expected_lambdas(h, a, neutral=True, warn=False))
                   for h, a in fixtures)

    print("reference fixtures: %d" % len(fixtures))
    print("slate mean total: %.4f -> %.4f (target %.2f)" % (pre, post, args.target))
    print("mu: %.5f -> %.5f (delta %+.5f)" % (old_mu, dc.mu, delta))

    if args.dry_run:
        return

    # Surgical in-place rewrite: update only mu (+ gamma mirror is unchanged),
    # stamp provenance, preserve every other key/value and ordering.
    raw["mu"] = dc.mu
    raw["level_target"] = args.target
    raw["level_delta_mu"] = delta
    raw["level_pre_mu"] = old_mu
    raw["level_note"] = (
        "mu shifted by +%.5f to anchor the WC2026 neutral slate mean total to "
        "%.2f goals/match (recent World-Cup base rate); supremacy log-ratio / "
        "raw 1X2 difference invariant. See "
        "docs/research/wca_alpha_2026/08_xg_and_totals.md." % (delta, args.target)
    )
    with open(DC_JSON, "w") as fh:
        json.dump(raw, fh, indent=2)
        fh.write("\n")
    print("wrote %s" % DC_JSON)


if __name__ == "__main__":
    main()
