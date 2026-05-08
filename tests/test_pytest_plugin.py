"""Unit tests for django_pg_baseline.pytest_plugin."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_pg_baseline import pytest_plugin as plugin_module
from django_pg_baseline.conf import BaselineConfig


def _patch_django_setup(monkeypatch, calls):
    import django

    monkeypatch.setattr(django, "setup", lambda: calls.append(True))


def _patch_get_config(monkeypatch, cfg_or_exc):
    from django_pg_baseline import conf as conf_module

    if isinstance(cfg_or_exc, Exception):

        def raiser():
            raise cfg_or_exc

        monkeypatch.setattr(conf_module, "get_config", raiser)
    else:
        monkeypatch.setattr(conf_module, "get_config", lambda: cfg_or_exc)


def _patch_install(monkeypatch, calls):
    from django_pg_baseline import patches as patches_module

    monkeypatch.setattr(
        patches_module,
        "install_test_db_patch",
        lambda c: calls.append(c),
    )


def test_pytest_configure_no_django_settings_module_is_noop(monkeypatch):
    """When DJANGO_SETTINGS_MODULE is unset, the plugin must stay out
    of the way — pytest may have picked us up transitively in a
    non-Django project.
    """
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    setup_calls = []
    _patch_django_setup(monkeypatch, setup_calls)

    plugin_module.pytest_configure(config=None)
    assert setup_calls == []


def test_pytest_configure_installs_patch(monkeypatch, tmp_path: Path):
    sql = tmp_path / "baseline.sql"
    sql.write_text("-- dump\n")
    cfg = BaselineConfig(baseline_dir=tmp_path, auto_load_on_test_db=True)

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")
    setup_calls: list[bool] = []
    install_calls: list = []
    _patch_django_setup(monkeypatch, setup_calls)
    _patch_get_config(monkeypatch, cfg)
    _patch_install(monkeypatch, install_calls)

    plugin_module.pytest_configure(config=None)
    assert setup_calls == [True]
    assert install_calls == [cfg]


def test_pytest_configure_skips_when_auto_load_disabled(monkeypatch, tmp_path: Path):
    """``auto_load_on_test_db=False`` opts out — even a missing SQL
    file should not raise. User has explicitly taken responsibility.
    """
    cfg = BaselineConfig(baseline_dir=tmp_path, auto_load_on_test_db=False)

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")
    setup_calls: list = []
    install_calls: list = []
    _patch_django_setup(monkeypatch, setup_calls)
    _patch_get_config(monkeypatch, cfg)
    _patch_install(monkeypatch, install_calls)

    plugin_module.pytest_configure(config=None)
    assert install_calls == []


def test_pytest_configure_raises_when_sql_missing(monkeypatch, tmp_path: Path):
    cfg = BaselineConfig(baseline_dir=tmp_path, auto_load_on_test_db=True)

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")
    setup_calls: list = []
    _patch_django_setup(monkeypatch, setup_calls)
    _patch_get_config(monkeypatch, cfg)

    with pytest.raises(pytest.UsageError, match="baseline.sql is missing"):
        plugin_module.pytest_configure(config=None)


def test_pytest_configure_silent_when_pg_baseline_unset(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")
    setup_calls: list = []
    install_calls: list = []
    _patch_django_setup(monkeypatch, setup_calls)
    _patch_get_config(monkeypatch, ImproperlyConfigured("nope"))
    _patch_install(monkeypatch, install_calls)

    plugin_module.pytest_configure(config=None)
    assert install_calls == []
