"""Unit tests for the lazy psycopg backend abstraction."""

from __future__ import annotations

import sys
import types

import pytest

from django_pg_baseline import _backend


def test_load_prefers_psycopg_v3(monkeypatch):
    """``psycopg`` v3 is preferred when available."""
    fake_v3 = types.SimpleNamespace(name="psycopg-v3", OperationalError=Exception)
    monkeypatch.setitem(sys.modules, "psycopg", fake_v3)
    assert _backend._load() is fake_v3


def test_load_falls_back_to_psycopg2(monkeypatch):
    """When ``psycopg`` is absent, fall back to ``psycopg2``."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    fake_v2 = types.SimpleNamespace(name="psycopg2", OperationalError=Exception)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_v2)

    real_import_module = _backend.importlib.import_module

    def fake_import_module(name):
        if name == "psycopg":
            raise ImportError("not installed")
        return real_import_module(name)

    monkeypatch.setattr(_backend.importlib, "import_module", fake_import_module)
    assert _backend._load() is fake_v2


def test_connect_passes_kwargs(monkeypatch):
    captured = {}

    class FakeDriver:
        OperationalError = Exception

        def connect(self, **kwargs):
            captured.update(kwargs)
            return "conn"

    monkeypatch.setattr(_backend, "_load", lambda: FakeDriver())

    result = _backend.connect(
        host="example.com",
        port="5433",
        user="me",
        password="pw",
        dbname="mydb",
    )
    assert result == "conn"
    assert captured == {
        "host": "example.com",
        "port": 5433,
        "user": "me",
        "password": "pw",
        "dbname": "mydb",
    }


def test_connect_normalises_missing_values(monkeypatch):
    captured = {}

    class FakeDriver:
        OperationalError = Exception

        def connect(self, **kwargs):
            captured.update(kwargs)
            return "conn"

    monkeypatch.setattr(_backend, "_load", lambda: FakeDriver())

    _backend.connect(host=None, port=None, user=None, password=None, dbname="db")
    assert captured["host"] == "localhost"
    assert captured["port"] == 5432
    assert captured["user"] == ""
    assert captured["password"] == ""


def test_operational_error_cls_returns_driver_class(monkeypatch):
    class CustomError(Exception):
        pass

    fake = types.SimpleNamespace(OperationalError=CustomError)
    monkeypatch.setattr(_backend, "_load", lambda: fake)

    cls = _backend.operational_error_cls()
    assert cls is CustomError
    with pytest.raises(CustomError):
        raise CustomError("boom")
