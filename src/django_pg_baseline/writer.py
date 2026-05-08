"""Build ``baseline.meta.json`` from the current source tree."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

META_VERSION = 1
"""Schema version for ``baseline.meta.json``.

Bumped when the file format changes incompatibly. Readers must check
``meta_version <= MAX_SUPPORTED`` and fail with a clear message
otherwise — see :func:`read_meta`.
"""

MAX_SUPPORTED_META_VERSION = 1


def _git_sha() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _postgres_version() -> str | None:
    from django.db import connection

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("Could not read Postgres version for meta.json")
        return None


def collect_last_migrations() -> dict[str, str]:
    """Return ``{app_label: last_migration_name}`` from disk."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    by_app: dict[str, list[str]] = {}
    for app_label, name in loader.disk_migrations:
        by_app.setdefault(app_label, []).append(name)
    return {app: max(names) for app, names in sorted(by_app.items())}


def write_meta(meta_path: Path) -> None:
    """Write a fresh ``baseline.meta.json`` next to the dump."""
    meta = {
        "meta_version": META_VERSION,
        "git_sha": _git_sha(),
        "postgres_version": _postgres_version(),
        "last_migration": collect_last_migrations(),
    }
    meta_path = Path(meta_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(f"[baseline] wrote {meta_path}")


def read_meta(meta_path: Path) -> dict:
    """Load ``baseline.meta.json`` and validate its ``meta_version``.

    Pre-``meta_version`` files (written by 0.0.x prereleases or by the
    in-tree BPP version that predated this field) are accepted and
    treated as ``meta_version=1``.
    """
    raw = json.loads(Path(meta_path).read_text())
    version = raw.get("meta_version", 1)
    if version > MAX_SUPPORTED_META_VERSION:
        raise RuntimeError(
            f"baseline.meta.json at {meta_path} has meta_version={version}, "
            f"but this version of django-pg-baseline only supports up to "
            f"{MAX_SUPPORTED_META_VERSION}. Upgrade django-pg-baseline."
        )
    return raw
