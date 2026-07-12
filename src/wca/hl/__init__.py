"""Hyperliquid HIP-4 outcome-market layer (READ-ONLY, monitor/shadow).

Hyperliquid is a NEW venue: per the live-money gate in CLAUDE.md it gets NO
real money until price capture + CLV stamping + settlement automation exist.
This package deliberately ships only:

* :mod:`wca.hl.client`  — read-only ``POST /info`` client (no keys, no
  ``/exchange``, nothing signed, nothing placed).
* :mod:`wca.hl.xvenue`  — pure (offline-testable) cross-venue HL<->Polymarket
  pair map + fee-adjusted gap/arb math for the settlement-matched 2026 World
  Cup pairs, feeding the SHADOW-only ``site/hl_xvenue.json`` feed.

Full venue recon + the go/no-go criteria any future execution scaffold must
clear first: ``docs/research/hl_venue_recon_2026-07-09.md``.
"""
