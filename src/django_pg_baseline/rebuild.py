"""Orchestrate baseline regeneration using testcontainers.

Replaces the ad-hoc ``docker compose -f docker-compose.baseline.yml``
incantation many projects copy-paste into their Makefiles. Spins an
isolated Postgres in a testcontainer, runs ``migrate``, freezes
configurable timestamp columns, runs ``pg_dump`` *inside* the
container (to guarantee client/server major-version match), scrubs
PG-version-specific lines, and writes ``baseline.sql`` +
``baseline.meta.json``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
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


def rebuild_baseline(config: BaselineConfig) -> None:
    """Spin a fresh Postgres testcontainer and rebuild the baseline.

    Steps:

    1. Spin a fresh ``PostgresContainer(image=cfg.rebuild_image)``.
    2. Validate ``pg_dump`` is present in the image (fail fast).
    3. Swap ``connections.databases[cfg.database_alias]`` to point at
       the testcontainer for the duration of migrate. Necessary
       because data migrations commonly grab ``django.db.connection``
       directly, ignoring the ``database=`` arg passed to
       ``call_command("migrate", database=…)``.
    4. ``migrate(interactive=False)``.
    5. Freeze configured timestamp columns.
    6. ``pg_dump`` inside the container.
    7. Scrub PG-version-specific directives.
    8. Write ``baseline.meta.json``.
    9. Restore the original ``default`` connection in ``finally``.

    Replaces ``connections.databases[alias]`` only for the ``alias``
    configured by ``cfg.database_alias`` (default ``"default"``);
    other aliases are untouched.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        raise RuntimeError(
            "testcontainers is required for baseline_rebuild. "
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
                "Could not close prior connection for alias %s during rebuild",
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
            call_command("migrate", interactive=False, verbosity=1, database=alias)
            _freeze_timestamps(alias, config)
            connections[alias].close()

            db = {
                "USER": _REBUILD_DB_USER,
                "PASSWORD": _REBUILD_DB_PASSWORD,
                "NAME": _REBUILD_DB_NAME,
            }
            _run_pg_dump(container_id, db, config)
            _scrub_dump(config.sql_path)
            write_meta(config.meta_path)
        finally:
            try:
                connections[alias].close()
            except Exception:
                logger.exception(
                    "Could not close connection for alias %s after rebuild",
                    alias,
                )
            if hasattr(connections._connections, alias):
                delattr(connections._connections, alias)
            connections.databases[alias] = original_alias_settings


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
