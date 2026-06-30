"""Transfer pUSD from the bot EOA to the account-2 proxy. RUN BY THE USER.

    .venv/bin/python scripts/wca_pm_transfer.py            # $5 canary
    .venv/bin/python scripts/wca_pm_transfer.py --all      # remaining balance
    .venv/bin/python scripts/wca_pm_transfer.py --amount 50

Destination is PM2_PROXY from .env (the 'World-Cup-26' account's proxy wallet
0xd42e…7b31, resolved via Polymarket's profile API from its signer address).
pUSD held AT the proxy is what the Polymarket UI shows as Cash — verified on
the main account (0x86b4 held exactly the UI's $920).

Staging discipline: send the $5 canary first, CONFIRM the new account's UI
shows ~$5 Cash, only then run --all. If the canary doesn't appear within a
few minutes, STOP — funds at the proxy address are still recoverable (it is
the account's own deterministic wallet), but we investigate before sending
the rest.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from eth_account import Account

RPCS = ["https://polygon.drpc.org", "https://polygon-bor-rpc.publicnode.com"]
CHAIN = 137
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


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


def rpc(method, params, tries=4):
    last = None
    for i in range(tries):
        host = RPCS[i % len(RPCS)]
        try:
            r = requests.post(host, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                          "params": params}, timeout=25).json()
            if "result" in r:
                return r["result"]
            last = r.get("error")
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:80]
        time.sleep(2)
    raise RuntimeError("%s failed: %s" % (method, last))


def addr32(a):
    return a[2:].lower().rjust(64, "0")


def bal(owner):
    data = "0x70a08231" + addr32(owner)
    return int(rpc("eth_call", [{"to": PUSD, "data": data}, "latest"]), 16)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--amount", type=float, default=5.0, help="pUSD to send (default $5 canary)")
    parser.add_argument("--all", action="store_true", help="send the full remaining balance")
    args = parser.parse_args()

    _load_dotenv()
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    dest = os.environ.get("PM2_PROXY")
    if not key or not dest:
        print("need POLYMARKET_PRIVATE_KEY and PM2_PROXY in .env")
        return 2
    acct = Account.from_key(key)

    have = bal(acct.address)
    amount_units = have if args.all else int(round(args.amount * 1e6))
    if amount_units <= 0 or amount_units > have:
        print("balance %.6f pUSD; requested %.6f — abort" % (have / 1e6, amount_units / 1e6))
        return 1

    print("sending %.6f pUSD  %s -> %s" % (amount_units / 1e6, acct.address, dest))
    data = "0xa9059cbb" + addr32(dest) + hex(amount_units)[2:].rjust(64, "0")
    nonce = int(rpc("eth_getTransactionCount", [acct.address, "latest"]), 16)
    tx = {"chainId": CHAIN, "nonce": nonce, "to": PUSD, "value": 0, "data": data,
          "gas": 80000, "maxFeePerGas": 800_000_000_000,
          "maxPriorityFeePerGas": 40_000_000_000, "type": 2}
    raw = acct.sign_transaction(tx).raw_transaction.hex()
    h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    print("tx:", h)
    for _ in range(60):
        time.sleep(3)
        try:
            rcpt = rpc("eth_getTransactionReceipt", [h], tries=2)
        except RuntimeError:
            rcpt = None
        if rcpt:
            ok = int(rcpt["status"], 16) == 1
            print("status:", "OK" if ok else "REVERTED")
            if ok:
                print("EOA pUSD now: %.6f | proxy pUSD now: %.6f"
                      % (bal(acct.address) / 1e6, bal(dest) / 1e6))
                print("\nNow check the World-Cup-26 account UI — Cash should show the amount.")
            return 0 if ok else 1
    print("not mined in 3 min — check the hash on Polygonscan")
    return 1


if __name__ == "__main__":
    sys.exit(main())
