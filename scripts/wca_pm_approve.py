#!/usr/bin/env python3
"""Set Polymarket proxy approvals via the gasless relayer.

The trading proxy is an ERC-1967 **DepositWallet** (NOT a Gnosis Safe) and
currently has ZERO pUSD allowances to both V2 exchanges, so self-signed CLOB
orders cannot settle.  This script signs a deposit-wallet ``Batch`` wallet-action
(owner-EOA-signed) and submits it through the Polymarket relayer's WALLET branch
to grant, in a single batch:

  1. pUSD.approve(standard exchange, MAX)
  2. pUSD.approve(neg-risk exchange, MAX)
  3. pUSD.approve(conditional-tokens, MAX)        [collateral for splits]
  4. CTF.setApprovalForAll(<exchanges>, true)     [ERC-1155 outcome tokens]

LIVE-MONEY DISCIPLINE
=====================
This script is DRY-RUN by default: it prints the exact action payloads it WOULD
submit and performs NO network writes.  To actually submit you must set BOTH:

    PM_APPROVE_LIVE=1   (environment)
    --yes               (CLI flag)

Anything less prints the preview and exits non-zero on the live path so a
half-armed invocation never leaks through.  Keys and signatures are never
logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

# Make the in-repo package importable when run as a standalone script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.pm import relayer  # noqa: E402


# Each planned action: (label, target_contract, calldata, operation).
def _planned_actions() -> List[Dict[str, Any]]:
    return [
        {
            "label": "pUSD.approve -> standard exchange",
            "to": relayer.PUSD_TOKEN,
            "data": relayer.build_approve_calldata(relayer.EXCHANGE_STD),
            "kind": "approve",
        },
        {
            "label": "pUSD.approve -> neg-risk exchange",
            "to": relayer.PUSD_TOKEN,
            "data": relayer.build_approve_calldata(relayer.EXCHANGE_NEG_RISK),
            "kind": "approve",
        },
        {
            "label": "pUSD.approve -> conditional-tokens",
            "to": relayer.PUSD_TOKEN,
            "data": relayer.build_approve_calldata(relayer.CTF_ADDRESS),
            "kind": "approve",
        },
        {
            "label": "CTF.setApprovalForAll -> standard exchange",
            "to": relayer.CTF_ADDRESS,
            "data": relayer.build_set_approval_for_all_calldata(
                relayer.EXCHANGE_STD, True
            ),
            "kind": "setApprovalForAll",
        },
        {
            "label": "CTF.setApprovalForAll -> neg-risk exchange",
            "to": relayer.CTF_ADDRESS,
            "data": relayer.build_set_approval_for_all_calldata(
                relayer.EXCHANGE_NEG_RISK, True
            ),
            "kind": "setApprovalForAll",
        },
    ]


def _print(line: str = "") -> None:
    print(line)


def main(
    argv: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    session: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="Set Polymarket proxy approvals.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm live submission (requires PM_APPROVE_LIVE=1 too).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=120.0,
        help="Seconds to poll relayer transaction status before giving up.",
    )
    args = parser.parse_args(argv)

    src_env = os.environ if env is None else env
    live_env = src_env.get("PM_APPROVE_LIVE") == "1"
    armed = live_env and args.yes

    client = relayer.RelayerClient(
        funder=relayer.DEPOSIT_WALLET,
        env=src_env,
        session=session,
        wallet_type=relayer.RelayerClient.WALLET_TYPE_WALLET,
    )

    _print("=" * 70)
    _print("Polymarket deposit-wallet approvals via relayer (WALLET branch)")
    _print("=" * 70)
    _print(f"  owner EOA     : {relayer.OWNER_EOA}")
    _print(f"  deposit wallet: {relayer.DEPOSIT_WALLET}")
    _print(f"  factory       : {relayer.FACTORY}")
    _print(f"  relayer       : {client.base_url}")
    _print(f"  pUSD token    : {relayer.PUSD_TOKEN}")
    _print()

    actions = _planned_actions()

    # Whether or not we submit, we always print the exact payloads.
    if not armed:
        _print("DRY-RUN (no network writes). Planned deposit-wallet Batch calls:")
        _print()
        for i, act in enumerate(actions, 1):
            _print(f"  [{i}] {act['label']}")
            _print(f"      kind  : {act['kind']}")
            _print(f"      target: {act['to']}")
            _print(f"      value : 0")
            _print(f"      data  : {act['data']}")
            _print()
        _print("  All calls are submitted as ONE Batch (single nonce/signature).")
        _print()
        if args.yes and not live_env:
            _print(
                "REFUSED: --yes given but PM_APPROVE_LIVE != 1. "
                "Set PM_APPROVE_LIVE=1 to arm live submission."
            )
            return 2
        if live_env and not args.yes:
            _print(
                "REFUSED: PM_APPROVE_LIVE=1 set but --yes flag missing. "
                "Pass --yes to arm live submission."
            )
            return 2
        _print(
            "To submit for real: set PM_APPROVE_LIVE=1 AND pass --yes. "
            "Default is dry-run."
        )
        return 0

    # --- LIVE PATH (both flags present) ----------------------------------
    _print("LIVE submission armed (PM_APPROVE_LIVE=1 and --yes).")
    _print()

    # All approvals go in ONE deposit-wallet Batch (one nonce, one signature).
    calls = [{"target": a["to"], "value": 0, "data": a["data"]} for a in actions]

    rc = 0
    try:
        nonce = client.get_nonce(address=relayer.OWNER_EOA, kind="WALLET")
        sig = client.sign_wallet_batch(calls, nonce)
        payload = client.build_wallet_payload(calls, nonce, sig)
        resp = client.submit_action(payload)
        tx_id = resp.get("transactionID") or resp.get("transactionId")
        _print(f"  submitted: tx={tx_id} state={resp.get('state')}")
        if tx_id:
            _poll(client, tx_id, args.poll_timeout)
    except relayer.RelayerError as exc:
        _print(f"  ERROR: {exc}")
        rc = 1

    _print()
    _print("On-chain allowance readback (public RPC):")
    for spender in (relayer.EXCHANGE_STD, relayer.EXCHANGE_NEG_RISK, relayer.CTF_ADDRESS):
        try:
            allowance = _read_allowance(
                relayer.PUSD_TOKEN, relayer.DEPOSIT_WALLET, spender
            )
            _print(f"  pUSD allowance {relayer.DEPOSIT_WALLET} -> {spender}: {allowance}")
        except Exception as exc:  # network/RPC errors are non-fatal to the report
            _print(f"  pUSD allowance -> {spender}: read failed ({exc})")

    return rc


# Public Polygon RPC used for the post-submit on-chain allowance readback.
_PUBLIC_RPC = "https://polygon-bor-rpc.publicnode.com"
_ALLOWANCE_SELECTOR = "dd62ed3e"  # allowance(address,uint256) -> allowance(owner,spender)


def _read_allowance(token: str, owner: str, spender: str) -> int:
    """eth_call ``allowance(owner, spender)`` on *token* via the public RPC.

    Pure-stdlib (urllib + json); returns the integer allowance.  Used only on
    the live path AFTER submit to confirm the approvals landed on-chain.
    """
    def _pad(addr: str) -> str:
        a = addr[2:] if addr.lower().startswith("0x") else addr
        return a.lower().rjust(64, "0")

    data = "0x" + _ALLOWANCE_SELECTOR + _pad(owner) + _pad(spender)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"],
    }
    req = urllib.request.Request(
        _PUBLIC_RPC,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted RPC)
        out = json.loads(resp.read().decode("utf-8"))
    result = out.get("result")
    if not result or result == "0x":
        return 0
    return int(result, 16)


def _poll(client: relayer.RelayerClient, tx_id: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get_transaction(tx_id)
        state = status.get("state")
        _print(f"      poll: state={state}")
        if state in ("STATE_CONFIRMED", "STATE_FAILED", "STATE_MINED"):
            return
        time.sleep(3)
    _print("      poll: timeout (did not confirm in window)")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
