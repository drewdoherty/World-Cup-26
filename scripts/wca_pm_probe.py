"""Polymarket connectivity + signing probe for World Cup Alpha.

Run this to verify that we can authenticate to the Polymarket CLOB and sign
orders correctly for *this* account — including proxy / deposit wallets that
trip up the official SDKs (clob-client-v2#65, py-clob-client-v2#70).

Behaviour
---------
* No ``POLYMARKET_PRIVATE_KEY`` in the environment / ``.env`` -> print exactly
  what to add and exit 0 (so CI and a fresh checkout stay green).
* Key present -> construct :class:`wca.pm.trader.ClobTrader`, derive L2 creds
  (L1 ClobAuth), detect the account class, read balance / allowance / open
  orders, fetch one live World Cup midpoint, and print a compact PROBE REPORT.
* Any auth failure exits non-zero with the CLOB's own error text — this is the
  signer-address bug detector.

The private key and every signature are NEVER printed or logged.

Usage::

    python scripts/wca_pm_probe.py
    python scripts/wca_pm_probe.py --env .env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Public EOA we expect for this account (public info; sanity-check only).
EXPECTED_EOA = "0x721A9E426267502d20bcB8afBe9db25a86dCEB76"


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (matches scripts/wca_bot.py); never echoes values."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_MISSING_KEY_MESSAGE = """\
POLYMARKET_PRIVATE_KEY is not set.

Add it to your .env (never commit it) so the probe can authenticate:

    POLYMARKET_PRIVATE_KEY=0x<your-64-hex-EOA-private-key>

Optional, for deposit / proxy-funded accounts (MetaMask or email/magic):

    POLYMARKET_FUNDER=0x<your-proxy-or-safe-wallet-address>
    POLYMARKET_SIG_TYPE=2        # 0=EOA (default), 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE

Then re-run:  python scripts/wca_pm_probe.py
"""


def _find_wc_token_id():
    """Return one (label, token_id) for a live WC market, or (None, None).

    Uses the existing read-only Gamma client; tolerant of schema drift and
    never raises into the probe.
    """
    try:
        from wca.data.polymarket import find_world_cup_markets
    except Exception:
        return None, None
    try:
        events = find_world_cup_markets(include_closed=False)
    except Exception as exc:  # network / API hiccup must not fail the probe
        print("  (could not load WC markets: %s)" % exc)
        return None, None

    import json as _json

    for ev in events:
        for mkt in ev.get("markets") or []:
            raw = mkt.get("clobTokenIds") or mkt.get("clob_token_ids")
            token_ids = raw
            if isinstance(raw, str):
                try:
                    token_ids = _json.loads(raw)
                except _json.JSONDecodeError:
                    token_ids = None
            if token_ids:
                label = (mkt.get("question") or ev.get("title") or "WC market")[:48]
                return label, str(token_ids[0])
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket signing/connectivity probe")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print(_MISSING_KEY_MESSAGE)
        return 0

    # Lazy import so the script still parses if the trader lands later.
    try:
        from wca.pm.trader import ClobTrader, ClobAuthError, resolve_funder_from_env
    except Exception as exc:
        print("ERROR: could not import ClobTrader: %s" % exc)
        return 2

    # Resolve the funder, falling back to the known proxy (Gnosis safe) — never
    # the empty EOA — when POLYMARKET_FUNDER is unset.  This makes the probe
    # report the account class an actual live order would use.
    funder, sig_type, used_fallback = resolve_funder_from_env()
    if used_fallback:
        print(
            "  NOTE: POLYMARKET_FUNDER unset — using known proxy %s (sig type %s)."
            % (funder, sig_type)
        )

    try:
        trader = ClobTrader(key, funder=funder, signature_type=sig_type)
    except Exception as exc:
        print("ERROR constructing trader: %s" % exc)
        return 2

    print("=" * 60)
    print("  POLYMARKET PROBE REPORT")
    print("=" * 60)
    addr = trader.address
    print("  EOA address     : %s" % addr)
    if addr.lower() != EXPECTED_EOA.lower():
        print("  WARNING: address does not match expected %s" % EXPECTED_EOA)

    # --- L1 -> creds (auth-failure = bug detector) -------------------------
    try:
        trader.derive_or_create_creds()
        print("  L1 ClobAuth     : OK (API creds derived)")
    except ClobAuthError as exc:
        print("  L1 ClobAuth     : FAILED")
        print("  %s" % exc)
        print("=" * 60)
        return 1
    except Exception as exc:
        print("  L1 ClobAuth     : ERROR %s" % exc)
        print("=" * 60)
        return 1

    # --- account class -----------------------------------------------------
    cls = trader.detect_account_class()
    print(
        "  Account class   : type %s (%s)"
        % (cls["signature_type"], cls["signature_type_name"])
    )
    print("  Funder (maker)  : %s" % cls["funder"])

    # --- balance / allowance (L2) -----------------------------------------
    try:
        ba = trader.balance_allowance()
        bal = ba.get("balance")
        allow = ba.get("allowance") if "allowance" in ba else ba.get("allowances")
        print("  USDC balance    : %s" % (bal if bal is not None else "?"))
        print("  USDC allowance  : %s" % (allow if allow is not None else "?"))
    except ClobAuthError as exc:
        print("  Balance         : FAILED %s" % exc)
        print("=" * 60)
        return 1
    except Exception as exc:
        print("  Balance         : ERROR %s" % exc)

    # --- open orders (L2) --------------------------------------------------
    try:
        orders = trader.open_orders()
        print("  Open orders     : %d" % len(orders))
    except Exception as exc:
        print("  Open orders     : ERROR %s" % exc)

    # --- one live WC midpoint (public) ------------------------------------
    label, token_id = _find_wc_token_id()
    if token_id:
        mid = trader.midpoint(token_id)
        if mid is not None:
            print("  Sample midpoint : %s = %.4f" % (label, mid))
        else:
            print("  Sample midpoint : %s = (no book yet)" % label)
    else:
        print("  Sample midpoint : (no live WC market with a token id found)")

    print("=" * 60)
    print("  Probe complete. Signing path verified for this account class.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
