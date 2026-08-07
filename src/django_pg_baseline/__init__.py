"""django_pg_baseline — fast bootstrap of Django test databases from a pg_dump.

Reusable Django app: installs a monkey patch on Django's test database
creation so a baseline pg_dump is loaded immediately after CREATE
DATABASE, turning hundreds of migrations into a few-second psql import.

Public API (stable from v0.1, the contract surface for downstream
tooling such as ``pytest-testcontainers-django``):

- :func:`get_baseline_path` — resolve the dump path to load.

Other helpers (``BaselineConfig``, ``install_test_db_patch``,
``load_baseline``, ``check_freshness`` …) are reachable via submodules
but are not yet locked under semver — see ``conf.py``, ``patches.py``,
``loader.py`` etc. They are stabilised at v1.0.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["get_baseline_path"]
__version__ = "0.3.1"


def get_baseline_path(alias: str = "default") -> Path | None:
    """Return the path to ``baseline.sql``, or ``None`` when not configured.

    Resolution order:

    1. ``DJANGO_PG_BASELINE_SQL_PATH`` environment variable, if set.
       Points directly at the dump file. The file does not need to
       exist for this branch — ``None`` is returned if it doesn't,
       matching the "configured but missing" semantics.
    2. ``settings.PG_BASELINE['BASELINE_DIR'] / SQL_FILENAME`` via
       :func:`django_pg_baseline.conf.get_config`.

    Returns ``None`` when:

    - ``settings.PG_BASELINE`` is unset or empty,
    - the resolved file does not exist on disk.

    This is the documented contract surface for
    ``pytest-testcontainers-django`` and any other downstream tooling
    that needs to know whether a baseline dump is available without
    coupling itself to our settings schema.

    The ``alias`` argument is reserved for the v2 multi-database
    extension (see SPEC §3.6) — in v1 it is accepted but ignored.
    """
    env_override = os.environ.get("DJANGO_PG_BASELINE_SQL_PATH")
    if env_override:
        path = Path(env_override)
        return path if path.exists() else None

    try:
        from django.core.exceptions import ImproperlyConfigured
    except ImportError:
        return None

    try:
        from .conf import get_config
    except ImportError:
        return None

    try:
        cfg = get_config()
    except ImproperlyConfigured:
        return None

    return cfg.sql_path if cfg.sql_path.exists() else None
