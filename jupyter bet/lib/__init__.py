"""jupyter bet — transparent research library over the WCA production stack.

Every module here REUSES the repo's production logic (``src/wca``,
``scripts/``) rather than re-implementing it: de-vigging comes from
``wca.markets.devig``, sizing from ``wca.markets.bankroll`` / ``wca.card``,
the decision pipeline from ``scripts/wca_betrecs.py``, arb math from
``wca.arbfx``, promo locks from ``wca.boostlock``, team canonicalisation from
``wca.data.teamnames`` — so what you inspect in the notebooks IS what
production does.

Import order matters only in that :mod:`lib.bootstrap` must run first (it
puts ``src/`` and ``scripts/`` on ``sys.path``); every other module imports
it at the top, so ``import lib.anything`` just works inside the notebooks.

Safety: nothing in this package places orders, signs transactions, touches
``pm_parked``/the trader, or writes to any ledger database. All SQLite access
is read-only. API keys are read from the repo ``.env`` by the existing
clients and are never printed.
"""
