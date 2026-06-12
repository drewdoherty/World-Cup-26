"""One-shot diagnostic: POST a single Polymarket order and print the VERBOSE
server response. RUN BY THE USER (with VPN OFF, Bahrain IP).

    .venv/bin/python scripts/wca_pm_try.py            # dry: build + show, no POST
    .venv/bin/python scripts/wca_pm_try.py --post      # actually POST ($2 Canada)

Why this exists: the bot relays only "400 Invalid order payload". This prints
the raw HTTP status + full body so we can see the REAL reason, and it tries
BOTH exchange domains (neg-risk and standard) since gamma's negRisk flag may
not match the CLOB's routing for single-match markets.

Signature is already proven valid on-chain (isValidSignature magic value), so
this isolates the wire/routing problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


# Canada win (PM-6/11) — small, liquid, settles tonight.
TOKEN = "94742454704773778167568688948405770414791647129006368131301932311609594298387"
PRICE = 0.53
SIZE = 3.77  # ~$2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="actually submit (else dry build only)")
    ap.add_argument("--token", default=TOKEN)
    ap.add_argument("--price", type=float, default=PRICE)
    ap.add_argument("--size", type=float, default=SIZE)
    args = ap.parse_args()

    _load_dotenv()
    from wca.pm.trader import ClobTrader

    t = ClobTrader(os.environ["PM2_PRIVATE_KEY"], funder=os.environ["PM2_PROXY"],
                   signature_type=3)
    t.derive_or_create_creds()

    host = t.config.host.rstrip("/")
    # All variants sign against the NEG-RISK domain (market confirmed neg-risk:
    # the standard-domain attempt returned "signature does not match order
    # hash"). We vary only the POST envelope to find the structural field the
    # neg-risk path requires.
    signed = t.build_order(token_id=args.token, side="BUY", price=args.price,
                           size=args.size, neg_risk=True)
    base = {
        "order": signed,
        "owner": t._creds.api_key if t._creds else t.address,
        "orderType": "GTC",
        "deferExec": False,
        "postOnly": False,
    }
    variants = [
        ("envelope negRisk=true (camel)", {**base, "negRisk": True}),
        ("envelope neg_risk=true (snake)", {**base, "neg_risk": True}),
        ("order.negRisk=true", {**base, "order": {**signed, "negRisk": True}}),
        ("plain (no flag)", base),
    ]
    for label, body in variants:
        print("\n=== variant: %s ===" % label)
        if not args.post:
            print("  (dry) keys:", list(body.keys()))
            continue
        try:
            payload = json.dumps(body, separators=(",", ":"))
            hdrs = t.l2_headers("POST", "/order", body=payload)
            r = requests.post(host + "/order", headers=hdrs, data=payload, timeout=20)
            print("  HTTP", r.status_code)
            print("  BODY", r.text[:600])
            if r.status_code == 200:
                print("\n*** ORDER ACCEPTED — winning envelope: %s ***" % label)
                return 0
        except Exception as exc:  # noqa: BLE001
            print("  request error:", str(exc)[:200])
    return 0 if not args.post else 1


if __name__ == "__main__":
    sys.exit(main())
