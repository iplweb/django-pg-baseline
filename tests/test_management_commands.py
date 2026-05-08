"""Tests for django_pg_baseline management commands."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from django_pg_baseline.conf import BaselineConfig
from django_pg_baseline.management.commands import baseline_info as cmd_info
from django_pg_baseline.management.commands import baseline_load as cmd_load
from django_pg_baseline.management.commands import baseline_rebuild as cmd_rebuild


def _patch_config(monkeypatch, cfg):
    monkeypatch.setattr(cmd_info, "get_config", lambda: cfg)
    monkeypatch.setattr(cmd_load, "get_config", lambda: cfg)
    monkeypatch.setattr(cmd_rebuild, "get_config", lambda: cfg)


def test_baseline_info_handles_missing_meta(tmp_path, monkeypatch, fake_sql_file):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)
    out = StringIO()
    # Informational only — never exits non-zero.
    call_command("baseline_info", stdout=out)
    text = out.getvalue()
    assert "missing" in text


def test_baseline_info_warns_when_sql_missing(tmp_path, monkeypatch, fake_meta_dict):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    cfg.meta_path.write_text(json.dumps(fake_meta_dict))
    _patch_config(monkeypatch, cfg)
    out = StringIO()
    call_command("baseline_info", stdout=out)
    text = out.getvalue()
    assert "baseline.sql is missing" in text


def test_baseline_info_happy_path(tmp_path, monkeypatch, fake_meta_dict, fake_sql_file):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    cfg.meta_path.write_text(json.dumps(fake_meta_dict))
    _patch_config(monkeypatch, cfg)

    from django_pg_baseline import freshness as freshness_module

    monkeypatch.setattr(
        freshness_module, "collect_disk_migrations", lambda: {"myapp": ["0050_initial"]}
    )

    out = StringIO()
    call_command("baseline_info", stdout=out)
    text = out.getvalue()
    assert "deadbeef" in text
    assert "PostgreSQL 16.0" in text
    assert "Worst delta" in text


class FakeCursor:
    def __init__(self, fetch_result):
        self._fetch_result = fetch_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        pass

    def fetchone(self):
        return self._fetch_result


class FakeConnection:
    def __init__(self, fetch_result):
        self._fetch_result = fetch_result
        self.settings_dict = {
            "HOST": "localhost",
            "PORT": 5432,
            "USER": "postgres",
            "PASSWORD": "",
            "NAME": "myapp_test",
        }

    def cursor(self):
        return FakeCursor(self._fetch_result)


def _patch_connections(monkeypatch, module, fake):
    class FakeConnections:
        def __getitem__(self, alias):
            return fake

    monkeypatch.setattr(module, "connections", FakeConnections())


def test_baseline_load_skips_when_populated(tmp_path, monkeypatch, fake_sql_file):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)
    _patch_connections(monkeypatch, cmd_load, FakeConnection(("django_migrations",)))

    load_calls = []
    monkeypatch.setattr(
        cmd_load, "load_baseline", lambda *a, **kw: load_calls.append(a)
    )

    out = StringIO()
    call_command("baseline_load", stdout=out)
    assert load_calls == []
    assert "skipping" in out.getvalue()


def test_baseline_load_invokes_loader_when_empty(tmp_path, monkeypatch, fake_sql_file):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)
    _patch_connections(monkeypatch, cmd_load, FakeConnection((None,)))

    load_calls = []
    monkeypatch.setattr(
        cmd_load, "load_baseline", lambda dsn, path: load_calls.append((dsn, path))
    )

    call_command("baseline_load", stdout=StringIO())
    assert len(load_calls) == 1
    assert load_calls[0][1] == cfg.sql_path


def test_baseline_load_force_skips_probe(tmp_path, monkeypatch, fake_sql_file):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)
    _patch_connections(monkeypatch, cmd_load, FakeConnection(("django_migrations",)))

    load_calls = []
    monkeypatch.setattr(
        cmd_load, "load_baseline", lambda dsn, path: load_calls.append((dsn, path))
    )

    call_command("baseline_load", "--force", stdout=StringIO())
    assert len(load_calls) == 1


def test_baseline_load_exits_when_sql_missing(tmp_path, monkeypatch):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)
    _patch_connections(monkeypatch, cmd_load, FakeConnection((None,)))

    def boom(dsn, path):
        raise FileNotFoundError("no dump")

    monkeypatch.setattr(cmd_load, "load_baseline", boom)

    err = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("baseline_load", stderr=err)
    assert exc.value.code == 1
    assert "no dump" in err.getvalue()


def test_baseline_rebuild_invokes_rebuild(tmp_path, monkeypatch):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)

    calls = []
    monkeypatch.setattr(cmd_rebuild, "rebuild_baseline", lambda c: calls.append(c))

    call_command("baseline_rebuild", stdout=StringIO())
    assert len(calls) == 1
    assert calls[0].rebuild_image == cfg.rebuild_image
    assert calls[0].baseline_dir == cfg.baseline_dir


def test_baseline_rebuild_image_override(tmp_path, monkeypatch):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)

    calls = []
    monkeypatch.setattr(cmd_rebuild, "rebuild_baseline", lambda c: calls.append(c))

    call_command("baseline_rebuild", "--image", "postgres:17", stdout=StringIO())
    assert calls[0].rebuild_image == "postgres:17"


def test_baseline_rebuild_baseline_dir_override(tmp_path, monkeypatch):
    cfg = BaselineConfig(baseline_dir=tmp_path)
    _patch_config(monkeypatch, cfg)

    calls = []
    monkeypatch.setattr(cmd_rebuild, "rebuild_baseline", lambda c: calls.append(c))

    other = tmp_path / "other"
    call_command("baseline_rebuild", "--baseline-dir", str(other), stdout=StringIO())
    assert calls[0].baseline_dir == other
