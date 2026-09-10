"""Shared fixtures.

``isolated_connections_file`` guarantees no test ever reads or writes the real
``~/.mysql-mcp/connections.json``; ``demo_profile`` stores a known-good profile
so the tool tests can address a connection by name, which is now the only way in.
"""

from __future__ import annotations

import pytest
from helpers import FakeConnection, FakeCursor

from mysql_mcp import connections, server


@pytest.fixture(autouse=True)
def isolated_connections_file(monkeypatch, tmp_path):
    path = tmp_path / "connections.json"
    monkeypatch.setenv("MYSQL_CONNECTIONS_FILE", str(path))
    yield path


@pytest.fixture
def demo_profile(isolated_connections_file):
    """Save a profile called ``demo`` and return its name."""

    def factory(name="demo", **overrides):
        fields = {"host": "demo-host", "database": "demo"}
        fields.update(overrides)
        connections.save_profile(name, fields)
        return name

    return factory


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the connection seam with a scripted cursor and return that cursor."""

    def factory(responses=None, database="demo"):
        cursor = FakeCursor(list(responses or []))
        conn = FakeConnection(cursor, database=database)
        monkeypatch.setattr(server, "_dial", lambda *args, **kwargs: conn)
        return cursor

    return factory
