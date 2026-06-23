"""GBP/USD FX rate — bounded live fetch with a cached fallback.

Monitoring-only: used to size the two legs of a cross-currency arb and report
guaranteed profit in a common currency. The live fetch is hard-bounded (short
timeout) and degrades to a sane fallback constant so it can NEVER hang or break
the pipeline. No key required (frankfurter.app is free + keyless).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Optional

# Fallback used when the live fetch fails/times out. USD per 1 GBP.
FALLBACK_USD_PER_GBP = 1.33
_TIMEOUT = 5.0
_URL = "https://api.frankfurter.app/latest?from=GBP&to=USD"


@dataclass(frozen=True)
class FxRate:
    usd_per_gbp: float
    source: str       # "live" | "fallback"
    asof: str = ""

    @property
    def gbp_per_usd(self) -> float:
        return 1.0 / self.usd_per_gbp if self.usd_per_gbp else 0.0


def get_gbp_usd(*, fetch=None) -> FxRate:
    """Return GBP→USD. ``fetch`` is injectable for tests (defaults to live).

    Never raises: any error/timeout yields the fallback rate.
    """
    fn = fetch if fetch is not None else _live_fetch
    try:
        rate, asof = fn()
        if rate and 0.5 < float(rate) < 3.0:  # sanity band
            return FxRate(float(rate), "live", asof or "")
    except Exception:  # noqa: BLE001 — fallback must never break the pipeline
        pass
    return FxRate(FALLBACK_USD_PER_GBP, "fallback", "")


def _live_fetch():
    req = urllib.request.Request(_URL, headers={"User-Agent": "wca-arb/monitoring"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return data["rates"]["USD"], data.get("date", "")
