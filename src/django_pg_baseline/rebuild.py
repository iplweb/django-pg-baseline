"""Orchestrate baseline regeneration using testcontainers.

Replaces the ad-hoc ``docker compose -f docker-compose.baseline.yml``
incantation many projects copy-paste into their Makefiles. Spins an
isolated Postgres in a testcontainer, runs ``migrate``, freezes
configurable timestamp columns, runs ``pg_dump`` *inside* the
container (to guarantee client/server major-version match), scrubs
PG-version-specific lines, and writes ``baseline.sql`` +
``baseline.meta.json``.

Two entry points share this pipeline:

- :func:`rebuild_baseline` — full reset. Runs ``migrate`` against an
  empty DB, capturing whatever auto-increment IDs ``post_migrate``
  happens to assign. Use when starting from scratch or recovering from
  a corrupted baseline.

- :func:`update_baseline` — incremental. Loads the existing
  ``baseline.sql`` into the testcontainer *before* ``migrate``, so
  Django's ``post_migrate`` (via ``update_contenttypes`` /
  ``create_permissions``, both built on ``get_or_create``) preserves
  the IDs of rows already present in the dump. Only genuinely-new
  permissions / content_types added by new migrations get fresh
  sequential IDs. This produces minimal git diffs on routine baseline
  refresh after adding migrations.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
import sys
from pathlib import Path

from .conf import BaselineConfig
from .writer import write_meta

logger = logging.getLogger(__name__)


# Vendor-neutral defaults. The actual values never escape the
# throwaway testcontainer, but using project-neutral names keeps the
# rebuild logs from leaking BPP-isms into OSS-side tracebacks.
_REBUILD_DB_USER = "postgres"
_REBUILD_DB_PASSWORD = "postgres"
_REBUILD_DB_NAME = "baseline"


def _freeze_timestamps(alias: str, config: BaselineConfig) -> None:
    from django.db import connections

    value = config.freeze_timestamp_value
    with connections[alias].cursor() as cur:
        for table, columns in config.freeze_timestamps:
            cur.execute(
                "SELECT to_regclass(%s)",
                [f"public.{table}"],
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                continue
            set_clause = ", ".join(f"{col} = %s::timestamptz" for col in columns)
            cur.execute(
                f"UPDATE {table} SET {set_clause}",
                [value] * len(columns),
            )


def _validate_pg_dump_in_container(container_id: str) -> None:
    """Fail fast when the rebuild image lacks ``pg_dump``.

    Cut-down/distroless images sometimes ship the server but not the
    client tools. Running ``pg_dump --version`` here surfaces that
    pre-migrate, instead of failing cryptically after a long migrate
    run.
    """
    try:
        subprocess.run(
            ["docker", "exec", container_id, "pg_dump", "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise RuntimeError(
            "pg_dump is not available inside the rebuild image. "
            "Pick an image that ships PostgreSQL client tools "
            f"(stderr: {stderr.strip()})."
        ) from exc


def _load_baseline_into_container(container_id: str, db: dict, sql_path: Path) -> None:
    """Stream a baseline SQL file into the container's PG via ``psql`` stdin.

    Used by :func:`update_baseline` to seed the testcontainer with the
    prior dump before running ``migrate``. Streaming via stdin avoids
    the two-step ``docker cp`` + ``docker exec`` dance and keeps the
    file off the container filesystem.

    psql output (rows of ``SET``, ``setval``, etc.) is captured and
    re-emitted to stderr only if the command fails — avoids the wall
    of result rows on success.
    """
    cmd = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={db.get('PASSWORD') or ''}",
        container_id,
        "psql",
        "-h",
        "localhost",
        "-p",
        "5432",
        "-U",
        str(db["USER"]),
        "-d",
        str(db["NAME"]),
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "--quiet",
    ]
    with sql_path.open("rb") as fh:
        result = subprocess.run(
            cmd,
            check=False,
            stdin=fh,
            stdout=subprocess.PIPE,
        )
    if result.returncode != 0:
        sys.stderr.buffer.write(result.stdout)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout
        )


def _run_pg_dump(container_id: str, db: dict, config: BaselineConfig) -> None:
    """Run ``pg_dump`` *inside* the testcontainer.

    The host's ``pg_dump`` may be a different major version than the
    server inside the container — and ``pg_dump`` emits version-
    specific preamble (e.g. PG17 adds ``SET transaction_timeout =
    0;``) which then makes the dump unrestorable on the older PG major
    we may target. Running ``pg_dump`` in-container guarantees
    client/server version match.
    """
    cmd = [
        "docker",
        "exec",
        "-e",
        f"PGPASSWORD={db.get('PASSWORD') or ''}",
        container_id,
        "pg_dump",
        "-h",
        "localhost",
        "-p",
        "5432",
        "-U",
        str(db["USER"]),
        "-d",
        str(db["NAME"]),
        "--format=plain",
        "--encoding=UTF8",
        *config.pg_dump_extra_args,
    ]
    config.sql_path.parent.mkdir(parents=True, exist_ok=True)
    with config.sql_path.open("wb") as fh:
        subprocess.run(cmd, check=True, stdout=fh)


def _scrub_dump(sql_path: Path) -> None:
    """Remove lines that break determinism or cross-major compatibility.

    - ``\\restrict`` / ``\\unrestrict``: psql meta-commands with random
      tokens emitted by newer ``pg_dump`` — non-deterministic, harmless
      to drop.
    - ``SET transaction_timeout = 0;``: emitted by ``pg_dump >= 17`` but
      unknown to PostgreSQL 16, which we still target as a possible
      baseline runtime. Leaving it in makes the dump unrestorable on
      PG16.

    This is treated as a living list of known incompatibilities; new
    PG majors may add directives we'll keep up with.
    """
    drop_patterns = [
        re.compile(r"^\\(un)?restrict "),
        re.compile(r"^SET transaction_timeout = "),
    ]
    text = sql_path.read_text(encoding="utf-8")
    kept = [
        line
        for line in text.splitlines(keepends=True)
        if not any(p.match(line) for p in drop_patterns)
    ]
    sql_path.write_text("".join(kept), encoding="utf-8")


def _build_db_settings(host: str, port: int, user: str, password: str, db: str) -> dict:
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db,
        "USER": user,
        "PASSWORD": password,
        "HOST": host,
        "PORT": str(port),
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {},
        "TIME_ZONE": None,
        "TEST": {},
    }


def _baseline_pipeline(config: BaselineConfig, *, load_prior: bool) -> None:
    """Shared rebuild/update pipeline.

    When ``load_prior`` is True, loads the existing ``baseline.sql``
    into the container before ``migrate``. The caller is responsible
    for verifying the file exists.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        raise RuntimeError(
            "testcontainers is required for baseline rebuild/update. "
            "Reinstall django-pg-baseline (testcontainers[postgres] is a "
            "regular runtime dependency)."
        ) from exc

    from django.core.management import call_command
    from django.db import connections

    container = PostgresContainer(
        image=config.rebuild_image,
        username=_REBUILD_DB_USER,
        password=_REBUILD_DB_PASSWORD,
        dbname=_REBUILD_DB_NAME,
        driver=None,
    )
    alias = config.database_alias
    with container as pg:
        host = pg.get_container_host_ip()
        port = int(pg.get_exposed_port(5432))
        container_id = pg.get_wrapped_container().id

        _validate_pg_dump_in_container(container_id)

        # Redirect the configured connection at the testcontainer for
        # the duration of migrate. Many projects' data migrations call
        # helpers that grab ``from django.db import connection`` —
        # which always returns the *default* connection, ignoring the
        # ``database=`` arg passed to ``call_command``. Without this
        # swap, those RunPython operations would silently execute
        # against the developer's local DB.
        original_alias_settings = connections.databases[alias]
        try:
            connections[alias].close()
        except Exception:
            logger.exception(
                "Could not close prior connection for alias %s during pipeline",
                alias,
            )
        # Evict the cached DatabaseWrapper so the next access rebuilds
        # it against the new settings_dict.
        if hasattr(connections._connections, alias):
            delattr(connections._connections, alias)
        connections.databases[alias] = _build_db_settings(
            host, port, _REBUILD_DB_USER, _REBUILD_DB_PASSWORD, _REBUILD_DB_NAME
        )

        try:
            db = {
                "USER": _REBUILD_DB_USER,
                "PASSWORD": _REBUILD_DB_PASSWORD,
                "NAME": _REBUILD_DB_NAME,
            }
            if load_prior:
                # Seed the testcontainer with the prior dump. ``migrate``
                # then sees existing django_migrations records and only
                # applies the delta; ``post_migrate`` sees existing
                # content_types/permissions via ``get_or_create`` and
                # preserves their IDs. New rows for newly-introduced
                # models append at the end of the sequence.
                _load_baseline_into_container(container_id, db, config.sql_path)

            call_command("migrate", interactive=False, verbosity=1, database=alias)
            _freeze_timestamps(alias, config)
            connections[alias].close()

            _run_pg_dump(container_id, db, config)
            _scrub_dump(config.sql_path)
            write_meta(config.meta_path)
        finally:
            try:
                connections[alias].close()
            except Exception:
                logger.exception(
                    "Could not close connection for alias %s after pipeline",
                    alias,
                )
            if hasattr(connections._connections, alias):
                delattr(connections._connections, alias)
            connections.databases[alias] = original_alias_settings


def rebuild_baseline(config: BaselineConfig) -> None:
    """Spin a fresh Postgres testcontainer and rebuild the baseline from scratch.

    Steps:

    1. Spin a fresh ``PostgresContainer(image=cfg.rebuild_image)``.
    2. Validate ``pg_dump`` is present in the image (fail fast).
    3. Swap ``connections.databases[cfg.database_alias]`` to point at
       the testcontainer for the duration of migrate. Necessary
       because data migrations commonly grab ``django.db.connection``
       directly, ignoring the ``database=`` arg passed to
       ``call_command("migrate", database=…)``.
    4. ``migrate(interactive=False)`` against an empty DB.
    5. Freeze configured timestamp columns.
    6. ``pg_dump`` inside the container.
    7. Scrub PG-version-specific directives.
    8. Write ``baseline.meta.json``.
    9. Restore the original ``default`` connection in ``finally``.

    Auto-increment IDs of permissions / content_types end up at
    whatever values ``post_migrate`` happens to assign — typically
    drifts between rebuilds, producing large diffs in ``baseline.sql``.
    For routine refreshes after adding migrations, prefer
    :func:`update_baseline` which preserves prior IDs.

    Replaces ``connections.databases[alias]`` only for the ``alias``
    configured by ``cfg.database_alias`` (default ``"default"``);
    other aliases are untouched.
    """
    _baseline_pipeline(config, load_prior=False)


def update_baseline(config: BaselineConfig) -> None:
    """Update ``baseline.sql`` in place, preserving auto-increment IDs.

    Loads the existing ``baseline.sql`` into a fresh testcontainer
    *before* ``migrate``, so:

    - ``django_migrations`` already records the migrations baked into
      the prior dump; ``migrate`` applies only the delta added since.
    - ``post_migrate`` fires for each app and runs
      ``update_contenttypes`` / ``create_permissions``. Both are built
      on ``get_or_create``, so existing content_type / permission rows
      keep their IDs. Only genuinely-new rows (for models added in
      the new migrations) get fresh sequential IDs at the end of each
      table's sequence.
    - All other auto-increment-bearing rows from the prior dump (e.g.
      seed data inserted by `RunPython` ops) survive untouched, since
      their migrations are recorded as already applied.

    The result: rebuilds with no migration changes produce a
    byte-identical ``baseline.sql`` (modulo the freeze-timestamp pass
    on ``django_migrations``); rebuilds adding new migrations produce a
    minimal diff confined to the actually-new rows.

    Use this for routine maintenance after adding migrations. Use
    :func:`rebuild_baseline` (full reset) when starting from scratch
    or when the prior dump is no longer loadable (e.g. after a Django
    or Postgres major version bump that broke the dump format).
    """
    if not config.sql_path.exists():
        raise FileNotFoundError(
            f"baseline_update requires an existing {config.sql_path}; "
            f"run baseline_rebuild first to create it."
        )
    _baseline_pipeline(config, load_prior=True)


def with_overrides(
    config: BaselineConfig,
    *,
    rebuild_image: str | None = None,
    baseline_dir: Path | None = None,
) -> BaselineConfig:
    """Return a new :class:`BaselineConfig` with selected fields replaced.

    Convenience wrapper around :func:`dataclasses.replace`; CLI command
    handlers use this to apply ``--image``/``--baseline-dir`` flags
    without mutating the shared config.
    """
    overrides: dict = {}
    if rebuild_image is not None:
        overrides["rebuild_image"] = rebuild_image
    if baseline_dir is not None:
        overrides["baseline_dir"] = Path(baseline_dir)
    return dataclasses.replace(config, **overrides)
