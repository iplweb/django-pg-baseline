"""Optional pytest plugin channel for installing the baseline patch.

The default integration path is to add ``"django_pg_baseline"`` to
``INSTALLED_APPS`` and let :class:`AppConfig.ready` install the
monkey patch. This plugin exists for downstream users who prefer not
to add the app to ``INSTALLED_APPS`` — both routes are idempotent
(double install is safe), but in practice you should pick one.

The plugin no-ops gracefully when there is no Django context (e.g.
``DJANGO_SETTINGS_MODULE`` unset because pytest picked us up
transitively in a non-Django project) but fails loudly when there
*is* a Django context with ``PG_BASELINE`` configured but the dump
file is missing — matching the ``AppConfig.ready()`` policy.
"""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        # Not a Django project — leave it alone. We may have been
        # picked up transitively via the pytest11 entry point.
        return

    import django

    django.setup()

    import pytest
    from django.core.exceptions import ImproperlyConfigured

    from .conf import get_config
    from .patches import install_test_db_patch

    try:
        cfg = get_config()
    except ImproperlyConfigured:
        # PG_BASELINE not set — fine.
        return

    if not cfg.auto_load_on_test_db:
        # User explicitly opted out of the auto-patch — don't validate
        # further, since they've taken responsibility for the wiring.
        return

    if not cfg.sql_path.exists():
        raise pytest.UsageError(
            "django-pg-baseline: BASELINE_DIR is configured "
            f"({cfg.baseline_dir}) but {cfg.sql_filename} is missing. "
            "Run `manage.py baseline_rebuild` to generate it, or "
            "remove PG_BASELINE from settings to disable."
        )

    install_test_db_patch(cfg)
