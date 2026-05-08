"""Thin abstraction over psycopg v2 / v3 — used only by ``patches.py``.

This package is a Django app, so the host project already has either
``psycopg2``, ``psycopg2-binary``, or ``psycopg[binary]>=3`` installed
(Django's Postgres backend requires one). We do not declare a runtime
dependency on either flavor — pinning one would conflict with the
host's choice.

The naive aliasing trick ``import psycopg2 as psycopg`` is deliberately
avoided — psycopg2 and psycopg3 are not API-compatible (connection-
string parsing, cursor methods, ``sql.SQL``, async story all differ).
This module documents the subset we actually rely on so future callers
do not accidentally introduce a v3-only feature and break v2 users.

The subset:

- ``connect(**kwargs)`` taking ``host``, ``port``, ``user``,
  ``password``, ``dbname`` keyword arguments;
- a connection object with ``.cursor()`` and ``.close()``;
- a cursor supporting context-manager protocol, ``.execute(sql)``,
  ``.fetchone()``;
- an ``OperationalError`` exception class on the module.

Imported lazily at patch-execution time, after Django has already
loaded its own DB driver — by then we know one of the two is
available.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def _load() -> ModuleType:
    """Return the psycopg module to use (v3 preferred, v2 fallback)."""
    try:
        return importlib.import_module("psycopg")
    except ImportError:
        return importlib.import_module("psycopg2")


def connect(
    *,
    host: str | None,
    port: int | str | None,
    user: str | None,
    password: str | None,
    dbname: str,
):
    """Open a Postgres connection using whichever psycopg is installed."""
    return _load().connect(
        host=host or "localhost",
        port=int(port) if port is not None else 5432,
        user=user or "",
        password=password or "",
        dbname=dbname,
    )


def operational_error_cls() -> type[BaseException]:
    """Return the installed driver's ``OperationalError`` class."""
    return _load().OperationalError
