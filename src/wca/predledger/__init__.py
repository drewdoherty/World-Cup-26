"""Prediction-ledger: a model-output book parallel to the money ledger.

Every model prediction (1X2 / scoreline / O-U / BTTS / advancement) gets one
deterministic row, settled against real results and (for 1X2) stamped with
de-vigged closing-line value.  Decoupling *what the model said* from *what was
bet* lets us measure model skill on a far larger sample than the handful of
real-money bets, while keeping the realized book joinable for an apples-to-
apples paper-vs-realized comparison.

Writes target ``data/dev.db`` on this dev box; the production ledger
``data/wca.db`` is read-only here and any attempt to write it raises unless
``WCA_ALLOW_PROD_DB`` is set (see :func:`wca.predledger.store._guard_db`).
"""
