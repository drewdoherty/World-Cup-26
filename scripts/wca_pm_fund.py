"""One-shot, staged funding of the Polymarket bot EOA. RUN BY THE USER.

    .venv/bin/python scripts/wca_pm_fund.py

What it does (in order, stopping dead on any revert or unexpected state):
  Stage 1  approve + wrap $1 USDC.e -> pUSD   (canary: proves the onramp
           mints exactly 1 pUSD to our EOA before more is at stake)
  Stage 2  approve + wrap the remaining USDC.e
  Stage 3  the five standing trading approvals:
           pUSD -> standard V2 exchange, neg-risk exchange, CTF (MAX), and
           CTF setApprovalForAll for both exchanges

Address provenance (verified 2026-06-12):
  - Onramp 0x93070a84...F5B8ee: official docs (docs.polymarket.com/concepts/pusd)
    + Polygonscan curated label "Polymarket: Permissionless Collateral Onramp"
    (verified source, deployed by Polymarket: Deployer 1, ~930k wrap txs).
  - Exchanges 0xE111...996B / 0xe2222...0F59: byte-verified against deployed
    contracts in docs/research/polymarket_v2_spec.md; live getCollateral()
    on both returns pUSD 0xC011a7E1...82DFB.
  - CTF 0x4D97...6045: canonical Gnosis ConditionalTokens used by Polymarket.

Reads POLYMARKET_PRIVATE_KEY from .env. Gas paid in POL (~$0.10 total).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
from eth_account import Account
from eth_hash.auto import keccak

RPCS = ["https://polygon.drpc.org", "https://polygon-bor-rpc.publicnode.com"]
CHAIN = 137
EOA = "0x721A9E426267502d20bcB8afBe9db25a86dCEB76"
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
EXCH = "0xE111180000d2663C0091e4f400237545B87B996B"
NEGRISK = "0xe2222d279d744050d28e00520010520000310F59"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
MAX = 2 ** 256 - 1

SEL_APPROVE = "0x095ea7b3"
SEL_SETALL = "0xa22cb465"
SEL_WRAP = "0x" + keccak(b"wrap(address,address,uint256)")[:4].hex()


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
            r = requests.post(
                host,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=25,
            ).json()
            if "result" in r:
                return r["result"]
            last = r.get("error")
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:80]
        time.sleep(2)
    raise RuntimeError("%s failed: %s" % (method, last))


def addr32(a):
    return a[2:].lower().rjust(64, "0")


def u256(n):
    return hex(n)[2:].rjust(64, "0")


def bal(token, owner):
    return int(rpc("eth_call", [{"to": token, "data": "0x70a08231" + addr32(owner)}, "latest"]), 16)


def main() -> int:
    _load_dotenv()
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("POLYMARKET_PRIVATE_KEY not in env/.env")
        return 2
    acct = Account.from_key(key)
    if acct.address.lower() != EOA.lower():
        print("key does not match expected EOA %s — abort" % EOA)
        return 2

    def send(label, to, data, gas=150000):
        # "latest" (not "pending") so a fee-starved stuck tx at this nonce is
        # REPLACED by this better-priced one instead of queueing behind it.
        nonce = int(rpc("eth_getTransactionCount", [acct.address, "latest"]), 16)
        # Polygon base fee spikes into the hundreds of gwei; cap generously —
        # only base+priority is actually paid (~$0.005/tx at 300 gwei).
        tx = {
            "chainId": CHAIN, "nonce": nonce, "to": to, "value": 0, "data": data,
            "gas": gas, "maxFeePerGas": 800_000_000_000,
            "maxPriorityFeePerGas": 40_000_000_000, "type": 2,
        }
        raw = acct.sign_transaction(tx).raw_transaction.hex()
        h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
        for _ in range(60):
            time.sleep(3)
            try:
                rcpt = rpc("eth_getTransactionReceipt", [h], tries=2)
            except RuntimeError:
                rcpt = None
            if rcpt:
                ok = int(rcpt["status"], 16) == 1
                print("%-36s %s gasUsed=%d %s" % (label, "OK" if ok else "REVERTED",
                                                  int(rcpt["gasUsed"], 16), h))
                if not ok:
                    sys.exit("STOP: %s reverted — nothing further was sent" % label)
                return
        sys.exit("STOP: %s not mined after 3 min: %s" % (label, h))

    usdce0, pusd0 = bal(USDCE, EOA), bal(PUSD, EOA)
    print("start: USDC.e=%.4f pUSD=%.4f" % (usdce0 / 1e6, pusd0 / 1e6))
    if usdce0 < 2_000_000:
        print("less than $2 USDC.e — nothing to do")
        return 1

    one = 1_000_000
    send("S1 approve onramp ($1)", USDCE, SEL_APPROVE + addr32(ONRAMP) + u256(one), gas=80000)
    send("S1 wrap $1 -> pUSD", ONRAMP, SEL_WRAP + addr32(USDCE) + addr32(EOA) + u256(one), gas=300000)
    got = bal(PUSD, EOA) - pusd0
    print("canary: +%.6f pUSD" % (got / 1e6))
    if got != one:
        sys.exit("STOP: canary minted %.6f, expected exactly 1.0" % (got / 1e6))

    rest = bal(USDCE, EOA)
    send("S2 approve onramp (rest)", USDCE, SEL_APPROVE + addr32(ONRAMP) + u256(rest), gas=80000)
    send("S2 wrap %.2f -> pUSD" % (rest / 1e6), ONRAMP,
         SEL_WRAP + addr32(USDCE) + addr32(EOA) + u256(rest), gas=300000)
    print("pUSD now: %.6f" % (bal(PUSD, EOA) / 1e6))

    for label, spender in [("S3 pUSD->exchange MAX", EXCH),
                           ("S3 pUSD->negrisk MAX", NEGRISK),
                           ("S3 pUSD->CTF MAX", CTF)]:
        send(label, PUSD, SEL_APPROVE + addr32(spender) + u256(MAX), gas=80000)
    for label, op in [("S3 CTF opAll exchange", EXCH), ("S3 CTF opAll negrisk", NEGRISK)]:
        send(label, CTF, SEL_SETALL + addr32(op) + u256(1), gas=80000)

    print("\nFINAL: pUSD=%.6f POL=%.4f" % (
        bal(PUSD, EOA) / 1e6, int(rpc("eth_getBalance", [EOA, "latest"]), 16) / 1e18))
    for nm, sp in [("exchange", EXCH), ("negrisk", NEGRISK), ("ctf", CTF)]:
        alw = int(rpc("eth_call", [{"to": PUSD, "data": "0xdd62ed3e" + addr32(EOA) + addr32(sp)}, "latest"]), 16)
        print("  pUSD allowance -> %-9s %s" % (nm, "MAX" if alw > 10 ** 30 else alw))
    print("\nDone. Next: I re-check the CLOB balance and park the first $1 order in Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
