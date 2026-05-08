"""Unit tests for django_pg_baseline.freshness."""

from __future__ import annotations

import json

import pytest

from django_pg_baseline import freshness as freshness_module
from django_pg_baseline.freshness import (
    FreshnessReport,
    check_freshness,
    collect_disk_migrations,
    compute_deltas,
)


def test_compute_deltas_new_app_counts_all():
    disk = {"newapp": ["0001_initial", "0002_add"]}
    baseline = {}
    assert compute_deltas(disk, baseline) == {"newapp": 2}


def test_compute_deltas_partial_delta():
    disk = {
        "myapp": ["0001_initial", "0002_x", "0003_y", "0004_z"],
    }
    baseline = {"myapp": "0002_x"}
    assert compute_deltas(disk, baseline) == {"myapp": 2}


def test_compute_deltas_no_new_migrations():
    disk = {"myapp": ["0001_initial", "0002_x"]}
    baseline = {"myapp": "0002_x"}
    assert compute_deltas(disk, baseline) == {"myapp": 0}


def test_compute_deltas_baseline_newer_than_disk():
    disk = {"myapp": ["0001_initial"]}
    baseline = {"myapp": "0009_future"}
    assert compute_deltas(disk, baseline) == {"myapp": 0}


def test_check_freshness_missing_meta_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="baseline.meta.json not found"):
        check_freshness(tmp_path / "missing.json")


def test_check_freshness_returns_report(tmp_path, monkeypatch):
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "meta_version": 1,
                "git_sha": "abc123",
                "last_migration": {"myapp": "0002_x"},
            }
        )
    )
    monkeypatch.setattr(
        freshness_module,
        "collect_disk_migrations",
        lambda: {"myapp": ["0001_initial", "0002_x", "0003_y"]},
    )

    report = check_freshness(meta_path)
    assert isinstance(report, FreshnessReport)
    assert report.deltas == {"myapp": 1}
    assert report.worst_app == "myapp"
    assert report.worst_delta == 1
    assert report.git_sha == "abc123"
    assert report.meta["git_sha"] == "abc123"


def test_check_freshness_handles_empty_baseline(tmp_path, monkeypatch):
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({"meta_version": 1, "last_migration": {}}))
    monkeypatch.setattr(
        freshness_module,
        "collect_disk_migrations",
        lambda: {"myapp": ["0001", "0002", "0003"]},
    )
    report = check_freshness(meta_path)
    assert report.deltas == {"myapp": 3}
    assert report.git_sha is None


def test_check_freshness_no_disk_migrations(tmp_path, monkeypatch):
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({"meta_version": 1, "last_migration": {}}))
    monkeypatch.setattr(freshness_module, "collect_disk_migrations", lambda: {})

    report = check_freshness(meta_path)
    assert report.deltas == {}
    assert report.worst_app == "(none)"
    assert report.worst_delta == 0


def test_check_freshness_pre_meta_version_file_accepted(tmp_path, monkeypatch):
    """Files written by 0.0.x prereleases lack ``meta_version`` —
    treat them as v1.
    """
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(
        json.dumps({"git_sha": "old", "last_migration": {"myapp": "0001"}})
    )
    monkeypatch.setattr(
        freshness_module,
        "collect_disk_migrations",
        lambda: {"myapp": ["0001"]},
    )
    report = check_freshness(meta_path)
    assert report.git_sha == "old"
    assert report.deltas == {"myapp": 0}


def test_check_freshness_unsupported_meta_version_raises(tmp_path):
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({"meta_version": 99, "last_migration": {}}))
    with pytest.raises(RuntimeError, match="meta_version=99"):
        check_freshness(meta_path)


def test_collect_disk_migrations_groups_and_sorts(monkeypatch):
    class FakeLoader:
        def __init__(self, connection=None, ignore_no_migrations=False):
            pass

        disk_migrations = [
            ("myapp", "0002_x"),
            ("myapp", "0001_initial"),
            ("auth", "0001_initial"),
        ]

    import django.db.migrations.loader as loader_mod

    monkeypatch.setattr(loader_mod, "MigrationLoader", FakeLoader)

    result = collect_disk_migrations()
    assert result == {
        "myapp": ["0001_initial", "0002_x"],
        "auth": ["0001_initial"],
    }
