"""Compute the migration delta between on-disk migrations and the baseline.

Informational only — the package does **not** enforce any threshold.
When to rebuild a baseline is the project's decision; this module just
makes the staleness visible (used by the ``baseline_info`` command).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .writer import read_meta


@dataclass
class FreshnessReport:
    """Summary of how far the baseline lags behind on-disk migrations."""

    deltas: dict[str, int]
    """Per-app count of migrations on disk newer than the baseline's recorded last migration."""

    git_sha: str | None
    """``git_sha`` from ``baseline.meta.json``."""

    worst_app: str
    """App label with the largest delta. ``"(none)"`` when no apps have migrations."""

    worst_delta: int
    """Largest per-app delta. ``0`` when no apps have migrations."""

    meta: dict = field(default_factory=dict)
    """Raw ``baseline.meta.json`` content for callers that want to inspect more."""


def collect_disk_migrations() -> dict[str, list[str]]:
    """Return ``{app_label: [migration_name, ...]}`` for on-disk migrations.

    Uses ``MigrationLoader.graph`` semantics — ``replaces=`` squash
    migrations are honored, so a squashed migration counts the new
    consolidated entry rather than its replaced ancestors.
    """
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    by_app: dict[str, list[str]] = {}
    for app_label, name in loader.disk_migrations:
        by_app.setdefault(app_label, []).append(name)
    for names in by_app.values():
        names.sort()
    return by_app


def compute_deltas(
    disk: dict[str, list[str]],
    baseline_last: dict[str, str],
) -> dict[str, int]:
    """Compute per-app counts of migrations on disk newer than baseline."""
    out: dict[str, int] = {}
    for app, names in disk.items():
        last = baseline_last.get(app)
        if last is None:
            out[app] = len(names)
        else:
            out[app] = sum(1 for n in names if n > last)
    return out


def check_freshness(meta_path: Path) -> FreshnessReport:
    """Build a :class:`FreshnessReport` from ``baseline.meta.json``.

    Raises :class:`FileNotFoundError` when the meta file is missing —
    the caller should advise the user to run ``manage.py
    baseline_rebuild`` or to remove ``PG_BASELINE`` from settings.
    """
    meta_path = Path(meta_path)
    if not meta_path.exists():
        raise FileNotFoundError(
            f"baseline.meta.json not found at {meta_path}. Run baseline_rebuild first."
        )
    meta = read_meta(meta_path)
    baseline_last: dict[str, str] = meta.get("last_migration", {})
    disk = collect_disk_migrations()
    deltas = compute_deltas(disk, baseline_last)
    if deltas:
        worst_app = max(deltas, key=lambda app: deltas[app])
        worst_delta = deltas[worst_app]
    else:
        worst_app = "(none)"
        worst_delta = 0
    return FreshnessReport(
        deltas=deltas,
        git_sha=meta.get("git_sha"),
        worst_app=worst_app,
        worst_delta=worst_delta,
        meta=meta,
    )
