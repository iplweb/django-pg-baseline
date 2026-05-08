"""Monkey-patch ``_create_test_db`` to preload a baseline pg_dump.

When pytest-django (or plain Django) creates a test database, we run
``psql -f baseline.sql`` against the freshly-empty DB so migrate only
applies the small delta of migrations added after the baseline was
dumped.

When ``DATABASES['default']['TEST']['TEMPLATE']`` is set (e.g. by
``pytest-testcontainers-django`` after mounting baseline.sql into the
PG init scripts), Django runs ``CREATE DATABASE … WITH TEMPLATE``
instead. Two distinct concerns then apply:

1. Postgres requires zero connections on the source database before
   ``WITH TEMPLATE`` is allowed. The patch boots Django's connection
   off the template and runs ``pg_terminate_backend`` against any
   leftover backends. This is generic Postgres hygiene — it runs
   whenever ``TEMPLATE`` is set, marker or no marker.

2. The packaging/coordination question of "did some external mechanism
   already seed the test DB?" is answered by the explicit
   ``_django_pg_baseline_seeded`` marker on
   ``DATABASES['default']['TEST']`` (set by the bridging package
   alongside ``TEMPLATE``). When ``TEMPLATE`` is set we treat the
   clone as already seeded and skip our own ``psql`` reload — whether
   or not the marker is set, since a user-managed ``TEMPLATE`` is
   also out of our remit. The marker is therefore informational in
   v0.x; it becomes load-bearing if v2 ever needs to distinguish
   "our partner seeded it" from "user-managed template".
"""

from __future__ import annotations

from .conf import BaselineConfig
from .loader import load_baseline

_already_patched = False
_SEEDED_MARKER = "_django_pg_baseline_seeded"


def install_test_db_patch(config: BaselineConfig) -> None:
    """Install (idempotently) the ``_create_test_db`` monkey patch.

    No-ops when ``config.sql_path`` does not exist — the
    ``AppConfig.ready()`` / pytest-plugin entry points already raise
    ``ImproperlyConfigured`` for the "configured but missing"
    scenario, so reaching this branch means the user has explicitly
    constructed a config without a dump file and we should stay out of
    the way.
    """
    global _already_patched
    if _already_patched:
        return
    if not config.sql_path.exists():
        return

    from django.db.backends.base import creation as _creation

    original = _creation.BaseDatabaseCreation._create_test_db

    def _create_test_db_with_baseline(self, verbosity, autoclobber, keepdb=False):
        dsn = self.connection.settings_dict
        test_settings = dsn.get("TEST") or {}
        template = test_settings.get("TEMPLATE")

        if template:
            # Generic Postgres hygiene: zero connections on source DB
            # before CREATE DATABASE WITH TEMPLATE.
            self.connection.close()
            close_pool = getattr(self.connection, "close_pool", None)
            if callable(close_pool):
                close_pool()
            with self._nodb_cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [template],
                )

        test_database_name = original(self, verbosity, autoclobber, keepdb)

        if template:
            # Clone path: either a partner package set the marker
            # (don't re-seed) or a user has set TEMPLATE for
            # unrelated reasons (don't second-guess them). Either
            # way: hands off the data.
            return test_database_name

        from . import _backend

        try:
            inspect = _backend.connect(
                host=dsn.get("HOST"),
                port=dsn.get("PORT"),
                user=dsn.get("USER"),
                password=dsn.get("PASSWORD"),
                dbname=test_database_name,
            )
        except _backend.operational_error_cls():
            # Test DB isn't reachable via the lazy backend — let
            # Django's normal migrate-from-scratch path take over.
            return test_database_name

        try:
            with inspect.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.django_migrations')")
                row = cursor.fetchone()
            empty = row is None or row[0] is None
        finally:
            inspect.close()

        if empty:
            load_baseline(
                {**dsn, "NAME": test_database_name},
                config.sql_path,
            )

        return test_database_name

    _creation.BaseDatabaseCreation._create_test_db = _create_test_db_with_baseline
    _already_patched = True
