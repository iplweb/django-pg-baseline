"""Django AppConfig — auto-installs the test-DB monkey patch on startup."""

from __future__ import annotations

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoPgBaselineConfig(AppConfig):
    name = "django_pg_baseline"
    verbose_name = "PostgreSQL baseline dump"

    def ready(self):
        from .conf import get_config
        from .patches import install_test_db_patch

        try:
            config = get_config()
        except ImproperlyConfigured:
            # PG_BASELINE not set at all — app is in INSTALLED_APPS but
            # the user hasn't configured it yet. Stay silent.
            return

        if not config.auto_load_on_test_db:
            return

        if not config.sql_path.exists():
            raise ImproperlyConfigured(
                "django-pg-baseline: BASELINE_DIR is configured "
                f"({config.baseline_dir}) but {config.sql_filename} is "
                "missing. Run `manage.py baseline_rebuild` to generate "
                "it, or remove PG_BASELINE from settings to disable."
            )

        install_test_db_patch(config)
