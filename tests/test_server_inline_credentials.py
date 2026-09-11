"""Inline credentials: dial first, remember only after the dial succeeded.

The password travels in the tool call exactly once. Everything after that goes
through the saved profile, which is why the failure modes matter as much as the
happy path: a failed dial must not leave a half-written profile behind, and an
existing name must not be quietly repointed at another server.
"""

from __future__ import annotations

import json

import pytest
from helpers import FakeConnection, FakeCursor, matches

from mysql_mcp import connections, server


def stored(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def dialed(monkeypatch):
    """Patch the dial seam; ``dialed.calls`` records every config it was handed."""
    calls: list[dict] = []

    def factory(responses=None, database="demo"):
        cursor = FakeCursor(list(responses or []))
        conn = FakeConnection(cursor, database=database)

        def fake_dial(config):
            calls.append(dict(config))
            return conn

        monkeypatch.setattr(server, "_dial", fake_dial)
        return cursor

    factory.calls = calls
    return factory


def test_a_successful_connect_saves_the_profile(fake_db, isolated_connections_file):
    fake_db([(matches("SHOW TABLES"), [])])

    server.list_tables(
        connection="lab",
        credentials={
            "host": "10.0.0.5",
            "user": "root",
            "password": "s3cret",
            "database": "labdb",
        },
    )

    payload = stored(isolated_connections_file)

    assert payload["lab"]["host"] == "10.0.0.5"
    assert payload["lab"]["password"] == "s3cret"
    assert payload["lab"]["database"] == "labdb"


def test_remember_false_runs_once_without_persisting_credentials(
    fake_db, isolated_connections_file
):
    fake_db([(matches("SHOW TABLES"), [])])

    result = server.list_tables(
        connection="one-shot",
        credentials={
            "host": "10.0.0.5",
            "user": "root",
            "password": "s3cret",
            "database": "labdb",
        },
        remember=False,
    )

    assert result == []
    assert not isolated_connections_file.exists()


def test_one_shot_credentials_do_not_need_a_connection_name(
    fake_db, isolated_connections_file
):
    fake_db([(matches("SHOW TABLES"), [])])

    result = server.list_tables(
        credentials={
            "host": "10.0.0.5",
            "user": "root",
            "password": "s3cret",
            "database": "labdb",
        },
        remember=False,
    )

    assert result == []
    assert not isolated_connections_file.exists()


def test_the_saved_profile_is_reused_without_credentials(
    fake_db, isolated_connections_file
):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])

    server.count_rows(
        "t",
        connection="lab",
        credentials={"host": "10.0.0.5", "database": "labdb"},
    )
    assert "lab" in stored(isolated_connections_file)

    second = server.count_rows("t", connection="lab")

    assert second["ok"] is True
    assert server.resolve_connection("lab").config["host"] == "10.0.0.5"


def test_a_first_connect_reports_where_it_was_saved(fake_db, isolated_connections_file):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])

    result = server.count_rows(
        "t",
        connection="lab",
        credentials={"host": "10.0.0.5", "database": "labdb"},
    )

    assert result["ok"] is True
    assert result["connection_saved"] == str(isolated_connections_file)


def test_a_later_call_does_not_claim_to_have_saved_again(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])
    server.count_rows(
        "t",
        connection="lab",
        credentials={"host": "10.0.0.5", "database": "labdb"},
    )

    result = server.count_rows("t", connection="lab")

    assert "connection_saved" not in result


def test_a_rejected_dial_saves_nothing(monkeypatch, isolated_connections_file):
    def boom(config):
        raise server.MySQLError("Access denied for user 'root'")

    monkeypatch.setattr(server, "_dial", boom)

    result = server.list_tables(
        connection="lab",
        credentials={"host": "10.0.0.5", "database": "labdb"},
    )

    assert result["ok"] is False
    assert result["code"] == "QUERY_FAILED"
    assert not isolated_connections_file.exists()


def test_credentials_never_require_a_pre_existing_profile(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])

    result = server.count_rows(
        "t",
        connection="brandnew",
        credentials={"host": "10.0.0.5", "database": "labdb"},
    )

    assert result["ok"] is True


def test_unknown_key_in_credentials_is_reported(fake_db):
    fake_db([])

    result = server.count_rows(
        "t", connection="lab", credentials={"host": "10.0.0.5", "typo": 1}
    )

    assert result["ok"] is False
    assert "typo" in result["error"]


def test_credentials_without_a_host_are_refused(fake_db):
    fake_db([])

    result = server.count_rows("t", connection="lab", credentials={"user": "root"})

    assert result["ok"] is False
    assert "host" in result["error"]


def test_credentials_without_a_connection_name_are_refused(fake_db):
    fake_db([])

    result = server.count_rows("t", credentials={"host": "10.0.0.5"})

    assert result["ok"] is False
    assert "connection=<name>" in result["error"]


def test_repointing_an_existing_name_is_refused(dialed, isolated_connections_file):
    connections.save_profile("lab", {"host": "10.0.0.5", "database": "labdb"})
    dialed([])

    result = server.count_rows("t", connection="lab", credentials={"host": "10.0.0.9"})

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_CONFLICT"
    assert dialed.calls == []
    assert stored(isolated_connections_file)["lab"]["host"] == "10.0.0.5"


def test_the_same_host_updates_the_password_and_keeps_the_policy(dialed):
    connections.save_profile(
        "lab", {"host": "10.0.0.5", "password": "old", "read_only": True}
    )
    dialed([(matches("SELECT COUNT(*)"), [{"total": 1}])])

    result = server.count_rows(
        "t", connection="lab", credentials={"host": "10.0.0.5", "password": "new"}
    )

    assert result["ok"] is True
    profile = connections.load_profile("lab")[1]
    assert profile["password"] == "new"
    assert profile["read_only"] is True


def test_credentials_cannot_loosen_a_read_only_profile(dialed):
    connections.save_profile("locked", {"host": "10.0.0.5", "read_only": True})
    dialed([])

    result = server.insert_row(
        "t",
        {"a": 1},
        connection="locked",
        credentials={"host": "10.0.0.5", "read_only": False},
    )

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_READ_ONLY"
    assert dialed.calls == []


def test_credentials_do_not_carry_policy_keys_into_a_new_profile(dialed):
    dialed([])

    server.insert_row(
        "t",
        {"a": 1},
        connection="fresh",
        credentials={"host": "10.0.0.5", "read_only": False},
    )

    assert "read_only" not in connections.load_profile("fresh")[1]


def test_passwords_never_come_back_in_the_result(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])

    result = server.count_rows(
        "t",
        connection="lab",
        credentials={
            "host": "10.0.0.5",
            "password": "s3cret",
            "database": "labdb",
        },
    )

    assert "s3cret" not in json.dumps(result)


def test_save_connection_refuses_to_repoint_then_allows_it_with_overwrite(fake_db):
    fake_db([])

    assert server.save_connection(name="lab", host="10.0.0.5")["ok"] is True

    refused = server.save_connection(name="lab", host="10.0.0.9")
    assert refused["ok"] is False
    assert refused["code"] == "CONNECTION_CONFLICT"

    forced = server.save_connection(name="lab", host="10.0.0.9", overwrite=True)
    assert forced["ok"] is True
    assert connections.load_profile("lab")[1]["host"] == "10.0.0.9"
