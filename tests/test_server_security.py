from __future__ import annotations

import asyncio

import pytest
from helpers import executed_sql, matches

from mysql_mcp import server


def test_every_operation_requires_credentials(fake_db):
    cursor = fake_db()

    result = server.list_tables(None)

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
    assert "credentials" in result["error"]
    assert not cursor.executed


def test_credentials_are_used_for_only_the_current_dial(
    monkeypatch, fake_db, credentials
):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 1}])])
    calls = []
    original = server._dial

    def record(config):
        calls.append(dict(config))
        return original(config)

    monkeypatch.setattr(server, "_dial", record)
    result = server.count_rows("items", credentials=credentials)

    assert result["ok"] is True
    assert calls[0]["host"] == "demo-host"
    assert calls[0]["password"] == "secret"
    assert result["target"] == {
        "host": "demo-host",
        "port": 3306,
        "database": "demo",
    }


def test_fetch_table_caps_limit_and_binds_values(fake_db, credentials):
    cursor = fake_db([(matches("SELECT * FROM"), [{"id": 1}])])

    result = server.fetch_table(
        "devices",
        credentials=credentials,
        limit=99_999,
        where={"field": "id", "op": "=", "value": 1},
    )

    assert result["ok"] is True
    assert result["limit_applied"] == server.MAX_QUERY_LIMIT
    assert result["truncated"] is True
    sql, params = next(
        item for item in cursor.executed if item[0].startswith("SELECT *")
    )
    assert "`id` = %s" in sql
    assert params == [1, server.MAX_QUERY_LIMIT]


def test_fetch_table_rejects_sql_syntax_in_value(fake_db, credentials):
    cursor = fake_db()
    result = server.fetch_table(
        "devices",
        credentials=credentials,
        where={"field": "id", "op": "=", "value": "1; DROP TABLE devices"},
    )
    assert result["ok"] is False
    assert not cursor.executed


@pytest.mark.parametrize("limit", ["100", 100])
def test_numeric_limits_are_accepted(fake_db, credentials, limit):
    fake_db([(matches("SELECT * FROM"), [])])
    result = server.fetch_table("devices", credentials=credentials, limit=limit)
    assert result["ok"] is True
    assert result["limit_applied"] == 100


@pytest.mark.parametrize("limit", ["bad", "", 0, -1])
def test_invalid_limits_are_rejected(fake_db, credentials, limit):
    cursor = fake_db()
    result = server.fetch_table("devices", credentials=credentials, limit=limit)
    assert result["ok"] is False
    assert "limit" in result["error"]
    assert not cursor.executed


def test_fetch_table_reads_columns_from_rows(fake_db, credentials):
    cursor = fake_db([(matches("SELECT * FROM"), [{"id": 1, "name": "one"}])])
    result = server.fetch_table("devices", credentials=credentials)
    assert result["columns"] == ["id", "name"]
    assert not any("information_schema" in sql for sql in executed_sql(cursor))


def test_fetch_table_uses_schema_when_result_is_empty(fake_db, credentials):
    fake_db(
        [
            (matches("SELECT * FROM"), []),
            (matches("SELECT COLUMN_NAME"), [{"column_name": "id"}]),
        ]
    )
    result = server.fetch_table("devices", credentials=credentials)
    assert result["columns"] == ["id"]


def test_count_rows_accepts_structured_where(fake_db, credentials):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 7}])])
    result = server.count_rows(
        "devices",
        credentials=credentials,
        where={"field": "status", "op": "=", "value": "active"},
    )
    assert result["total"] == 7


def test_describe_missing_table_is_a_structured_error(fake_db, credentials):
    fake_db([(matches("SELECT COLUMN_NAME"), [])])
    result = server.describe_table("missing", credentials=credentials)
    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_health_reports_target_without_password(fake_db, credentials):
    fake_db(
        [
            (
                matches("SELECT DATABASE()"),
                [{"database_name": "demo", "server_version": "8.0.42"}],
            )
        ]
    )
    result = server.health_check(credentials)
    assert result["ok"] is True
    assert result["server_version"] == "8.0.42"
    assert "secret" not in str(result)


def test_driver_errors_are_returned_not_raised(monkeypatch, credentials):
    def fail(_config):
        raise server.MySQLError("connection refused")

    monkeypatch.setattr(server, "_dial", fail)
    result = server.get_databases(credentials)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


@pytest.mark.parametrize("name", ["db.users", "订单表", "", "1users", "user$info"])
def test_identifiers_are_restricted(name):
    with pytest.raises(ValueError):
        server._safe_identifier(name, "table_name")


def test_multiple_statements_are_rejected(fake_db, credentials):
    cursor = fake_db()
    result = server.execute_query("SELECT 1; SELECT 2", credentials=credentials)
    assert result["ok"] is False
    assert "single statement" in result["error"]
    assert not cursor.executed


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ';' AS s",
        "SELECT 1 # trailing; comment",
        "SELECT `a;b` FROM t",
        "SELECT 1 -- ; not a statement",
    ],
)
def test_semicolons_in_literals_and_comments_are_allowed(fake_db, credentials, sql):
    fake_db([(matches("SELECT"), [{"n": 1}])])
    assert server.execute_query(sql, credentials=credentials)["ok"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t INTO OUTFILE '/tmp/leak.csv'",
        "EXPLAIN ANALYZE DELETE FROM t",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t LOCK IN SHARE MODE",
    ],
)
def test_read_only_rejects_write_capable_statements(fake_db, credentials, sql):
    cursor = fake_db()
    result = server.execute_query(sql, credentials=credentials)
    assert result["ok"] is False
    assert not cursor.executed


def test_execute_query_write_requires_explicit_opt_out(fake_db, credentials):
    fake_db([(matches("DELETE"), [])])
    blocked = server.execute_query(
        "DELETE FROM t WHERE id = 1", credentials=credentials
    )
    allowed = server.execute_query(
        "DELETE FROM t WHERE id = 1", credentials=credentials, read_only=False
    )
    assert blocked["ok"] is False
    assert allowed["ok"] is True


def test_read_only_credentials_block_all_writes_before_dial(fake_db, credentials):
    cursor = fake_db()
    credentials["read_only"] = True

    result = server.insert_row("items", {"id": 1}, credentials=credentials)

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_READ_ONLY"
    assert not cursor.executed


def test_delete_refuses_oversized_mutation(fake_db, credentials):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(1500,)])])
    result = server.delete_rows(
        "items",
        where={"field": "id", "op": ">", "value": 0},
        credentials=credentials,
        max_affected_rows=100,
    )
    assert result["code"] == "MUTATION_LIMIT_EXCEEDED"
    assert not any(sql.startswith("DELETE") for sql in executed_sql(cursor))


def test_mutation_cap_cannot_exceed_hard_limit(fake_db, credentials):
    cursor = fake_db()
    result = server.delete_rows(
        "items",
        where={"field": "id", "op": ">", "value": 0},
        credentials=credentials,
        max_affected_rows=10**9,
    )
    assert result["ok"] is False
    assert str(server.MAX_MUTATION_ROWS) in result["error"]
    assert not cursor.executed


def test_credentials_can_lower_but_not_raise_mutation_cap(fake_db, credentials):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(5,)])])
    credentials["max_affected_rows"] = 3
    result = server.update_rows(
        "items",
        {"status": "done"},
        where={"field": "id", "op": ">", "value": 0},
        credentials=credentials,
        max_affected_rows=1000,
    )
    assert result["code"] == "MUTATION_LIMIT_EXCEEDED"
    assert result["max_affected_rows"] == 3
    assert not any(sql.startswith("UPDATE") for sql in executed_sql(cursor))


def test_query_timeout_is_applied(fake_db, credentials):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])
    server.count_rows("items", credentials=credentials)
    assert any(
        sql.startswith("SET SESSION MAX_EXECUTION_TIME") for sql in executed_sql(cursor)
    )


def test_where_helpers_reject_raw_sql_and_bad_logic():
    with pytest.raises(TypeError):
        server._build_where_clause("id = 1", None)
    with pytest.raises(ValueError, match="first where clause"):
        server._build_where_clause(
            [{"logic": "OR", "field": "id", "op": "=", "value": 1}], None
        )


def test_where_in_operator_binds_each_value():
    sql, params = server._build_where_clause(
        [{"field": "id", "op": "IN", "value": [1, 2, 3]}], None
    )
    assert sql == "`id` IN (%s, %s, %s)"
    assert params == [1, 2, 3]


def test_tls_options_reach_the_driver(monkeypatch, fake_db, credentials):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])
    calls = []
    original = server._dial

    def record(config):
        calls.append(dict(config))
        return original(config)

    monkeypatch.setattr(server, "_dial", record)
    credentials["ssl_ca"] = "/certs/ca.pem"
    credentials["ssl_verify_cert"] = True
    server.count_rows("items", credentials=credentials)
    assert calls[0]["ssl_ca"] == "/certs/ca.pem"
    assert calls[0]["ssl_verify_cert"] is True


def test_database_is_required_only_for_schema_operations(fake_db, credentials):
    credentials.pop("database")
    fake_db([(matches("SHOW TABLES"), [])])
    result = server.list_tables(credentials)
    assert result["ok"] is False
    assert "database" in result["error"]


def test_mcp_schema_exposes_only_stateless_tools():
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert names == {
        "health_check",
        "list_tables",
        "get_databases",
        "fetch_table",
        "count_rows",
        "insert_row",
        "update_rows",
        "delete_rows",
        "describe_table",
        "execute_query",
    }
    for tool in tools:
        properties = tool.input_schema["properties"]
        assert "credentials" in properties
        assert "credentials" in tool.input_schema["required"]
        assert "connection" not in properties
        assert "remember" not in properties


def test_registered_tools_do_not_claim_persistence():
    for tool in asyncio.run(server.mcp.list_tools()):
        text = f"{tool.description} {tool.input_schema}".lower()
        assert "save_connection" not in text
        assert "remember" not in text
