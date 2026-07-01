"""Isolated paper-trading 'test book'.

A fully separate, fake-money book (default $2000 USD) for an unproven
high-frequency strategy on exotic Polymarket match-event markets. It lives in its
own SQLite DB (``data/test_book.db``) and never touches the real ledger — it gets
no real funding until it proves itself.

Submodules:
* :mod:`wca.testbook.store`  — schema + paper-bet/bankroll/mark-to-market API.
* :mod:`wca.testbook.trader` — the automated paper-trader (model vs CLOB).
"""

from wca.testbook import store  # noqa: F401
