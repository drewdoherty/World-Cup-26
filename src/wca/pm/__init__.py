"""Polymarket CLOB trading — self-contained signer + REST client.

This package implements Polymarket order placement *without* the official
``py-clob-client`` / ``clob-client-v2`` SDKs, which carry an open bug for
deposit-wallet (proxy) accounts: the L1 auth flow binds the API key to the
signing EOA, but the order ``maker`` / ``signer`` fields and ``signatureType``
are mishandled for proxy-funded accounts
(Polymarket/clob-client-v2#65, py-clob-client-v2#70).  Because we control both
the request construction *and* the signature, we implement signing correctly
for every account class:

==========  ====================  ===========================  ====================
sig type    name                  ``maker`` (funds)            ``signer`` (key)
==========  ====================  ===========================  ====================
0           EOA                   EOA                          EOA
1           POLY_PROXY            proxy wallet (funder)        EOA
2           POLY_GNOSIS_SAFE      Gnosis safe (funder)         EOA
==========  ====================  ===========================  ====================

The only hard dependency beyond the stdlib + ``requests`` is ``eth-account``
(for EIP-712 typed-data signing).  ``web3`` and the Polymarket SDKs are *not*
required.

Modules
-------
:mod:`wca.pm.signing`
    Pure-function L1 EIP-712 auth signing, L2 HMAC signing, and EIP-712 order
    signing.  No network, no global state — every function is unit-testable
    with a throwaway key.
:mod:`wca.pm.trader`
    :class:`~wca.pm.trader.ClobTrader`, a thin REST client that wires the
    signers to the CLOB host: derive creds, detect account class, read
    balance / open orders / midpoints, and place orders (with a dry-run mode).
"""
from __future__ import annotations

from wca.pm.signing import (
    CTF_EXCHANGE,
    NEG_RISK_EXCHANGE,
    USDC_COLLATERAL,
    CTF_EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2,
    PUSD_COLLATERAL,
    EXCHANGE_V1,
    EXCHANGE_V2,
    POLYGON_CHAIN_ID,
    SIG_EOA,
    SIG_POLY_PROXY,
    SIG_POLY_GNOSIS_SAFE,
    OrderArgs,
    build_l1_headers,
    build_l2_headers,
    build_signed_order,
    build_order_hash,
    build_order_typed_data,
    verifying_contract_for,
)

__all__ = [
    "CTF_EXCHANGE",
    "NEG_RISK_EXCHANGE",
    "USDC_COLLATERAL",
    "CTF_EXCHANGE_V2",
    "NEG_RISK_EXCHANGE_V2",
    "PUSD_COLLATERAL",
    "EXCHANGE_V1",
    "EXCHANGE_V2",
    "POLYGON_CHAIN_ID",
    "SIG_EOA",
    "SIG_POLY_PROXY",
    "SIG_POLY_GNOSIS_SAFE",
    "OrderArgs",
    "build_l1_headers",
    "build_l2_headers",
    "build_signed_order",
    "build_order_hash",
    "build_order_typed_data",
    "verifying_contract_for",
]
