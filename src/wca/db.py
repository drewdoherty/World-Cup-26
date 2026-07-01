"""Single choke point for opening the WCA state database.

Historically every module opened its own ``sqlite3.connect("data/wca.db")``.
That forks the ledger the moment two machines write on different files. This
helper centralises the decision so a *single* environment switch moves the whole
platform onto a shared Turso (libSQL) database without touching call sites.

Selection rule
--------------
* ``WCA_DB_URL`` set (``libsql://…``)  -> connect via ``libsql_experimental``
  against the shared Turso database, authenticated with ``WCA_DB_AUTH_TOKEN``.
* ``WCA_DB_URL`` unset (dev, tests, and the mini until the operator deliberately
  cuts over) -> plain ``sqlite3.connect`` against a local file, exactly as
  before. This path is byte-for-byte behaviour-identical to the legacy code.

The libSQL client is an *optional* dependency: it is imported lazily and only
when ``WCA_DB_URL`` is set, so dev/test/CI installs never need it.

See ``docs/operations/turso_migration.md`` for the migration plan.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

# Default local SQLite path, kept in sync with ``wca.ledger.store._DEFAULT_DB``.
_DEFAULT_DB = "data/wca.db"


def connect(db_path: Optional[str] = None):
    """Open the WCA database.

    Parameters
    ----------
    db_path:
        Explicit path to a local SQLite file. Honoured ONLY on the sqlite
        fallback path (``WCA_DB_URL`` unset); when a Turso URL is configured the
        connection is always the shared remote database and ``db_path`` is
        ignored (there is no per-file forking to point at). When ``db_path`` is
        ``None`` the sqlite path resolves ``$WCA_DB_PATH`` and finally the
        ``data/wca.db`` default — matching the legacy ``sqlite3.connect`` calls.

    Returns
    -------
    A DB-API-ish connection. On the sqlite path this is a real
    :class:`sqlite3.Connection` with ``row_factory = sqlite3.Row`` set (so rows
    support both positional and by-name access, exactly as the ledger store
    expects). On the libSQL path it is a ``libsql_experimental`` connection.
    """
    url = os.environ.get("WCA_DB_URL")
    if url:
        # Optional dependency: only imported when a Turso URL is configured, so
        # dev/test/CI never require ``libsql-experimental`` to be installed.
        import libsql_experimental as libsql  # pip install libsql-experimental

        conn = libsql.connect(
            database=url,
            auth_token=os.environ["WCA_DB_AUTH_TOKEN"],
        )
        # Best-effort: give libSQL rows sqlite3.Row-style name access so callers
        # that read ``row["col"]`` keep working. The experimental client does not
        # currently expose a ``row_factory`` attribute the way sqlite3 does; if a
        # future version adds one, set it here. LIMITATION: until then, rows from
        # the libSQL path are positional tuples, NOT name-indexable like
        # sqlite3.Row. Callers that rely on ``row["col"]`` (e.g. the ledger store)
        # therefore need name access to be re-applied by the caller, or this
        # attribute to exist. This branch is currently UNEXERCISED in tests
        # because there is no real Turso instance to connect to in CI.
        try:  # pragma: no cover - depends on libSQL client capabilities
            conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
        except Exception:
            # Client does not support a settable row accessor; leave as-is.
            pass
        return conn

    # ---- sqlite fallback: identical to the legacy behaviour -----------------
    path = db_path or os.environ.get("WCA_DB_PATH") or _DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
