"""Tests for the top-level ``django_pg_baseline`` re-exports."""

from __future__ import annotations

from pathlib import Path

import django_pg_baseline
from django_pg_baseline import get_baseline_path


def test_top_level_only_exports_get_baseline_path():
    """Until v1.0 the only stable top-level export is ``get_baseline_path``.

    Other helpers are reachable via submodules but not re-exported, to
    leave room for refactoring the public API before locking it under
    semver.
    """
    assert "get_baseline_path" in django_pg_baseline.__all__


def test_get_baseline_path_returns_none_when_no_setting(settings):
    if hasattr(settings, "PG_BASELINE"):
        del settings.PG_BASELINE
    assert get_baseline_path() is None


def test_get_baseline_path_returns_none_when_file_missing(
    settings, tmp_baseline_dir: Path
):
    settings.PG_BASELINE = {"BASELINE_DIR": str(tmp_baseline_dir)}
    assert get_baseline_path() is None


def test_get_baseline_path_returns_path_when_present(
    settings, tmp_baseline_dir: Path, fake_sql_file: Path
):
    settings.PG_BASELINE = {"BASELINE_DIR": str(tmp_baseline_dir)}
    result = get_baseline_path()
    assert result == fake_sql_file


def test_env_override_used_when_set(monkeypatch, tmp_path: Path):
    dump = tmp_path / "elsewhere.sql"
    dump.write_text("-- elsewhere\n")
    monkeypatch.setenv("DJANGO_PG_BASELINE_SQL_PATH", str(dump))
    assert get_baseline_path() == dump


def test_env_override_returns_none_when_file_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DJANGO_PG_BASELINE_SQL_PATH", str(tmp_path / "missing.sql"))
    assert get_baseline_path() is None


def test_env_override_takes_precedence_over_settings(
    monkeypatch, settings, tmp_baseline_dir: Path, fake_sql_file: Path, tmp_path: Path
):
    elsewhere = tmp_path / "elsewhere.sql"
    elsewhere.write_text("-- env wins\n")
    settings.PG_BASELINE = {"BASELINE_DIR": str(tmp_baseline_dir)}
    monkeypatch.setenv("DJANGO_PG_BASELINE_SQL_PATH", str(elsewhere))
    assert get_baseline_path() == elsewhere
