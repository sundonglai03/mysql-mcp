"""Behaviour of the tools when several connections are in play."""

from __future__ import annotations

import json

import pytest
from helpers import executed_sql, matches

from mysql_mcp import connections, server


@pytest.fixture
def saved_profile(isolated_connections_file):
    """Store a profile on disk and return its name."""

    def factory(name="mydb", **overrides):
        fields = {
            "host": "10.0.0.9",
            "port": 3306,
            "user": "reader",
            "password": "pw",
            "database": "mydb",
        }
        fields.update(overrides)
        connections.save_profile(name, fields)
        return name

    return factory


def test_a_saved_profile_drives_the_driver_kwargs(saved_profile):
    saved_profile("warehouse", host="db.example.com", port=3307, database="analytics")

    config = server.get_connection_config("warehouse")

    assert config["host"] == "db.example.com"
    assert config["port"] == 3307
    assert config["database"] == "analytics"
    assert config["user"] == "reader"
    assert config["password"] == "pw"
    assert config["connection_timeout"] == server.DEFAULT_CONNECT_TIMEOUT


def test_a_profile_tls_block_reaches_the_driver(saved_profile):
    saved_profile("warehouse", ssl_ca="/profile/ca.pem")

    assert server.get_connection_config("warehouse")["ssl_ca"] == "/profile/ca.pem"


def test_read_only_profile_refuses_writes_before_dialling(fake_db, saved_profile):
    cursor = fake_db([(matches("SELECT"), [{"n": 1}])])
    saved_profile("warehouse", read_only=True)

    result = server.execute_query(
        "DELETE FROM t WHERE id = 1", read_only=False, connection="warehouse"
    )

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_READ_ONLY"
    assert cursor.executed == []


def test_read_only_profile_still_allows_reads(fake_db, saved_profile):
    fake_db([(matches("SELECT"), [{"n": 1}])])
    saved_profile("warehouse", read_only=True)

    result = server.execute_query("SELECT 1", connection="warehouse")

    assert result["ok"] is True
    assert result["connection"] == "warehouse"


@pytest.mark.parametrize(
    "call",
    [
        lambda name: server.delete_rows(
            "t", where=[{"field": "id", "op": "=", "value": 1}], connection=name
        ),
        lambda name: server.update_rows(
            "t",
            {"status": "x"},
            where=[{"field": "id", "op": "=", "value": 1}],
            connection=name,
        ),
        lambda name: server.insert_row("t", {"id": 1}, connection=name),
    ],
)
def test_read_only_profile_blocks_every_write_tool(fake_db, saved_profile, call):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(1,)])])
    saved_profile("warehouse", read_only=True)

    result = call("warehouse")

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_READ_ONLY"
    assert not any(
        sql.upper().startswith(("DELETE", "UPDATE", "INSERT"))
        for sql in executed_sql(cursor)
    )


def test_a_write_profile_allows_writes(fake_db, saved_profile):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(2,)])])
    saved_profile("warehouse", read_only=False)

    result = server.delete_rows(
        "t", where=[{"field": "id", "op": "=", "value": 1}], connection="warehouse"
    )

    assert result["ok"] is True
    assert any(sql.upper().startswith("DELETE FROM") for sql in executed_sql(cursor))


def test_a_profile_can_lower_the_mutation_cap(fake_db, saved_profile):
    fake_db([(matches("SELECT COUNT(*)"), [(5,)])])
    saved_profile("warehouse", max_affected_rows=3)

    result = server.delete_rows(
        "t", where=[{"field": "id", "op": "=", "value": 1}], connection="warehouse"
    )

    assert result["ok"] is False
    assert result["code"] == "MUTATION_LIMIT_EXCEEDED"
    assert result["max_affected_rows"] == 3
    assert result["matched_rows"] == 5


def test_the_caller_cannot_raise_a_profile_cap(fake_db, saved_profile):
    fake_db([(matches("SELECT COUNT(*)"), [(50,)])])
    saved_profile("warehouse", max_affected_rows=10)

    result = server.update_rows(
        "t",
        {"status": "x"},
        where=[{"field": "id", "op": "=", "value": 1}],
        max_affected_rows=1000,
        connection="warehouse",
    )

    assert result["ok"] is False
    assert result["max_affected_rows"] == 10


def test_a_profile_without_a_cap_uses_the_caller_default(fake_db, saved_profile):
    fake_db([(matches("SELECT COUNT(*)"), [(2,)])])
    saved_profile("warehouse")

    result = server.delete_rows(
        "t", where=[{"field": "id", "op": "=", "value": 1}], connection="warehouse"
    )

    assert result["ok"] is True
    assert result["max_affected_rows"] == server.DEFAULT_MAX_MUTATION_ROWS


def test_tools_report_which_connection_served_them(fake_db, saved_profile):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])
    saved_profile("warehouse")

    assert server.count_rows("t", connection="warehouse")["connection"] == "warehouse"


def test_health_check_reports_the_connection(fake_db, saved_profile):
    fake_db(
        [
            (
                matches("SELECT DATABASE()"),
                [{"database_name": "mydb", "server_version": "8.0.42"}],
            )
        ]
    )
    saved_profile("mydb", host="db.example.com", read_only=True)

    result = server.health_check("mydb")

    assert result["ok"] is True
    assert result["connection"] == "mydb"
    assert result["host"] == "db.example.com"
    assert result["read_only"] is True


def test_a_profile_without_a_database_says_so(fake_db, saved_profile):
    fake_db([(matches("SHOW TABLES"), [])])
    saved_profile("warehouse", database=None)

    result = server.list_tables(connection="warehouse")

    assert result["ok"] is False
    assert "no database selected" in result["error"]


def test_a_call_without_a_connection_is_refused_not_silently_local(
    fake_db, saved_profile
):
    """A bare `list_tables()` must not quietly dial localhost:3306."""
    cursor = fake_db([(matches("SHOW TABLES"), [])])
    saved_profile("warehouse")

    result = server.list_tables()

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
    assert "connection" in result["error"]
    assert cursor.executed == []


def test_the_missing_connection_error_lists_the_saved_names(fake_db, saved_profile):
    fake_db([])
    saved_profile("alpha")
    saved_profile("beta")

    result = server.count_rows("t")

    assert result["ok"] is False
    assert "alpha" in result["error"]
    assert "beta" in result["error"]


def test_the_missing_connection_error_says_so_when_the_file_is_empty(fake_db):
    fake_db([])

    result = server.count_rows("t")

    assert result["ok"] is False
    assert "save_connection" in result["error"]


def test_list_connections_has_no_implicit_default(saved_profile):
    saved_profile("warehouse")

    result = server.list_connections()

    assert result["ok"] is True
    assert "default" not in result
    assert "allowed_hosts" not in result
    assert "connection=" in result["usage"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.list_tables(connection="ghost"),
        lambda: server.count_rows("t", connection="ghost"),
        lambda: server.describe_table("t", connection="ghost"),
        lambda: server.execute_query("SELECT 1", connection="ghost"),
        lambda: server.delete_rows(
            "t", where=[{"field": "id", "op": "=", "value": 1}], connection="ghost"
        ),
    ],
)
def test_an_unknown_connection_is_a_structured_error(fake_db, call):
    fake_db([(matches("SELECT"), [])])

    result = call()

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
    assert "ghost" in result["error"]


def test_list_connections_shows_the_saved_ones(saved_profile):
    saved_profile("warehouse", password="topsecret")

    result = server.list_connections()

    assert result["ok"] is True
    assert [entry["name"] for entry in result["connections"]] == ["warehouse"]
    assert "topsecret" not in json.dumps(result)
    assert result["connections"][0]["has_password"] is True


def test_save_connection_tool_persists_a_usable_profile(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 2}])])

    saved = server.save_connection(
        "warehouse",
        host="db.example.com",
        port=3307,
        database="warehouse",
        password="pw",
        description="production",
    )

    assert saved["ok"] is True
    assert saved["saved"] == "warehouse"
    assert saved["has_password"] is True
    assert "password" not in saved["connection"]

    result = server.count_rows("t", connection="warehouse")

    assert result["ok"] is True
    assert result["connection"] == "warehouse"


def test_save_connection_applies_defaults():
    saved = server.save_connection("warehouse", host="h")

    assert saved["connection"]["port"] == connections.DEFAULT_PORT
    assert saved["connection"]["user"] == "root"
    assert saved["connection"]["charset"] == connections.DEFAULT_CHARSET
    assert saved["has_password"] is False
    assert "warning" not in saved


@pytest.mark.parametrize(
    ("name", "host"),
    [("bad name", "h"), ("_reserved", "h"), ("ok", "")],
)
def test_save_connection_rejects_bad_input(name, host):
    saved = server.save_connection(name, host=host)

    assert saved["ok"] is False
    assert saved["code"] == "CONNECTION_INVALID"


def test_delete_connection_tool_removes_the_profile(saved_profile):
    saved_profile("warehouse")

    result = server.delete_connection("warehouse")

    assert result["ok"] is True
    assert result["deleted"] == "warehouse"
    assert "password" not in result["connection"]
    assert server.list_connections()["connections"] == []


def test_delete_connection_rejects_an_unknown_name():
    result = server.delete_connection("ghost")

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_INVALID"


def test_a_deleted_connection_can_no_longer_be_used(fake_db, saved_profile):
    fake_db([(matches("SELECT"), [{"n": 1}])])
    saved_profile("warehouse")
    server.delete_connection("warehouse")

    result = server.execute_query("SELECT 1", connection="warehouse")

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
