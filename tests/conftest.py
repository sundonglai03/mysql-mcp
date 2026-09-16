"""Shared stateless MySQL test fixtures."""

from __future__ import annotations

import pytest
from helpers import FakeConnection, FakeCursor

from mysql_mcp import server


@pytest.fixture
def credentials():
    return {
        "host": "demo-host",
        "port": 3306,
        "user": "reader",
        "password": "secret",
        "database": "demo",
    }


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the only connection seam with a scripted fake database."""

    def factory(responses=None, database="demo"):
        cursor = FakeCursor(list(responses or []))
        conn = FakeConnection(cursor, database=database)
        monkeypatch.setattr(server, "_dial", lambda *args, **kwargs: conn)
        return cursor

    return factory
