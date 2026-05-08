"""Minimal Django settings for the test suite.

The unit tests do not actually open database connections — they
monkeypatch ``django.db.connection`` and ``MigrationLoader`` instead.
The DATABASES dict only needs to be valid enough that
``django.setup()`` doesn't reject it.
"""

from __future__ import annotations

SECRET_KEY = "django-pg-baseline-test-key-not-secret"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django_pg_baseline",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "django_pg_baseline_test",
        "USER": "postgres",
        "PASSWORD": "",
        "HOST": "localhost",
        "PORT": "5432",
        "TEST": {},
    }
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
