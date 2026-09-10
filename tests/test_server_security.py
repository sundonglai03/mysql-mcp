from __future__ import annotations

import asyncio

import pytest
from helpers import executed_sql, matches

from mysql_mcp import connections, server

DEMO = "demo"


@pytest.fixture(autouse=True)
def demo(demo_profile):
    """Every test in this module talks to a saved profile named ``demo``."""
    return demo_profile(DEMO)


def test_fetch_table_rejects_sql_injection_in_where(fake_db):
    cursor = fake_db([(matches("SELECT * FROM"), [{"id": 1}])])

    result = server.fetch_table(
        "DeviceBaseInfo",
        where={"field": "DeviceID", "op": "=", "value": "1; DROP TABLE DeviceBaseInfo"},
        connection=DEMO,
    )

    assert result["ok"] is False
    assert "where" in result["error"].lower()
    assert not cursor.executed


def test_fetch_table_caps_limit_and_flags_truncation(fake_db):
    cursor = fake_db([(matches("SELECT * FROM"), [{"DeviceID": 1}])])

    result = server.fetch_table(
        "DeviceBaseInfo",
        limit=10_000,
        where=[{"field": "DeviceID", "op": "=", "value": 1}],
        order_by=[{"field": "DeviceID", "direction": "ASC"}],
        connection=DEMO,
    )

    assert result["ok"] is True
    select_sql = next(sql for sql in executed_sql(cursor) if sql.startswith("SELECT *"))
    params = next(
        params for sql, params in cursor.executed if sql.startswith("SELECT *")
    )
    assert "LIMIT %s" in select_sql
    assert params[-1] == server.MAX_QUERY_LIMIT
    assert result["truncated"] is True
    assert result["limit_applied"] == server.MAX_QUERY_LIMIT


def test_limit_accepts_a_numeric_string(fake_db):
    cursor = fake_db([(matches("SELECT * FROM"), [])])

    result = server.fetch_table("DeviceBaseInfo", limit="100", connection=DEMO)

    assert result["ok"] is True
    assert result["limit_applied"] == 100
    assert result["truncated"] is False
    assert cursor.executed


def test_limit_string_above_the_cap_is_still_truncated(fake_db):
    fake_db([(matches("SELECT * FROM"), [])])

    result = server.fetch_table("DeviceBaseInfo", limit="99999", connection=DEMO)

    assert result["ok"] is True
    assert result["limit_applied"] == server.MAX_QUERY_LIMIT
    assert result["truncated"] is True


@pytest.mark.parametrize("bad_limit", ["abc", "", 0, -5])
def test_limit_rejects_non_positive_or_non_numeric(fake_db, bad_limit):
    fake_db([(matches("SELECT * FROM"), [])])

    result = server.fetch_table("DeviceBaseInfo", limit=bad_limit, connection=DEMO)

    assert result["ok"] is False
    assert "limit" in result["error"]


def test_fetch_table_skips_schema_lookup_when_rows_come_back(fake_db):
    cursor = fake_db([(matches("SELECT * FROM"), [{"id": 1}])])

    result = server.fetch_table("DeviceBaseInfo", connection=DEMO)

    assert result["columns"] == ["id"]
    assert not any("information_schema" in sql for sql in executed_sql(cursor))


def test_fetch_table_falls_back_to_schema_lookup_when_empty(fake_db):
    cursor = fake_db(
        [
            (matches("SELECT * FROM"), []),
            (
                matches("SELECT COLUMN_NAME"),
                [{"column_name": "id"}, {"column_name": "name"}],
            ),
        ]
    )

    result = server.fetch_table("DeviceBaseInfo", connection=DEMO)

    assert result["columns"] == ["id", "name"]
    assert any("information_schema" in sql for sql in executed_sql(cursor))


def test_count_rows_accepts_structured_where(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 7}])])

    result = server.count_rows(
        "DeviceBaseInfo",
        where=[{"field": "DeviceID", "op": "=", "value": 42}],
        connection=DEMO,
    )

    assert result["ok"] is True
    assert result["total"] == 7


def test_describe_table_returns_structured_error_when_missing(fake_db):
    fake_db([(matches("SELECT COLUMN_NAME"), [])])

    result = server.describe_table("MissingTable", connection=DEMO)

    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_health_check_reports_database_and_version(fake_db):
    fake_db(
        [
            (
                matches("SELECT DATABASE()"),
                [{"database_name": "warehouse", "server_version": "8.0.36"}],
            )
        ]
    )

    result = server.health_check(connection=DEMO)

    assert result["ok"] is True
    assert result["database"] == "warehouse"
    assert result["server_version"] == "8.0.36"


def test_list_tables_returns_structured_error_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise server.MySQLError("connection refused")

    monkeypatch.setattr(server, "_dial", boom)

    result = server.list_tables(connection=DEMO)

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_get_databases_returns_structured_error_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise server.MySQLError("connection refused")

    monkeypatch.setattr(server, "_dial", boom)

    result = server.get_databases(connection=DEMO)

    assert isinstance(result, dict)
    assert result["ok"] is False


def test_multiple_statements_are_rejected(fake_db):
    fake_db([(matches("SELECT"), [{"n": 1}])])

    result = server.execute_query("SELECT 1; SELECT 2", connection=DEMO)

    assert result["ok"] is False
    assert "single statement" in result["error"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ';' AS s",
        "SELECT 1 # trailing; comment",
        "SELECT `a;b` FROM t",
        "SELECT 1 -- ; not a statement",
    ],
)
def test_semicolons_in_literals_or_comments_are_not_multi_statements(fake_db, sql):
    fake_db([(matches("SELECT"), [{"n": 1}])])

    result = server.execute_query(sql, connection=DEMO)

    assert result["ok"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t INTO OUTFILE '/tmp/leak.csv'",
        "SELECT * FROM t INTO DUMPFILE '/tmp/leak.bin'",
        "EXPLAIN ANALYZE DELETE FROM t",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t LOCK IN SHARE MODE",
    ],
)
def test_read_only_rejects_write_capable_statements(fake_db, sql):
    fake_db([(matches("SELECT"), [])])

    result = server.execute_query(sql, connection=DEMO)

    assert result["ok"] is False
    assert "read_only" in result["error"]


def test_read_only_allows_a_plain_select(fake_db):
    fake_db([(matches("SELECT"), [{"n": 1}])])

    result = server.execute_query("SELECT 1", connection=DEMO)

    assert result["ok"] is True


def test_write_sql_requires_explicit_opt_out(fake_db):
    fake_db([(matches("DELETE"), [])])

    blocked = server.execute_query("DELETE FROM t WHERE id = 1", connection=DEMO)
    assert blocked["ok"] is False

    allowed = server.execute_query(
        "DELETE FROM t WHERE id = 1", read_only=False, connection=DEMO
    )
    assert allowed["ok"] is True


def test_delete_rows_refuses_oversized_mutation(fake_db):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(1500,)])])

    result = server.delete_rows(
        "DeviceBaseInfo",
        where=[{"field": "DeviceID", "op": "=", "value": 1}],
        max_affected_rows=100,
        connection=DEMO,
    )

    assert result["ok"] is False
    assert result["code"] == "MUTATION_LIMIT_EXCEEDED"
    assert result["matched_rows"] == 1500
    assert not any(sql.upper().startswith("DELETE") for sql in executed_sql(cursor))


def test_update_rows_refuses_oversized_mutation(fake_db):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(9999,)])])

    result = server.update_rows(
        "DeviceBaseInfo",
        {"status": "on"},
        where=[{"field": "DeviceID", "op": "=", "value": 1}],
        connection=DEMO,
    )

    assert result["ok"] is False
    assert result["code"] == "MUTATION_LIMIT_EXCEEDED"
    assert not any(sql.upper().startswith("UPDATE") for sql in executed_sql(cursor))


def test_delete_rows_proceeds_under_the_cap(fake_db):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [(3,)])])

    result = server.delete_rows(
        "DeviceBaseInfo",
        where=[{"field": "DeviceID", "op": "=", "value": 1}],
        connection=DEMO,
    )

    assert result["ok"] is True
    assert result["matched"] == 3
    assert any(sql.upper().startswith("DELETE FROM") for sql in executed_sql(cursor))


def test_mutation_cap_cannot_exceed_the_hard_cap(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [(1,)])])

    result = server.delete_rows(
        "DeviceBaseInfo",
        where=[{"field": "DeviceID", "op": "=", "value": 1}],
        max_affected_rows=10**9,
        connection=DEMO,
    )

    assert result["ok"] is False
    assert str(server.MAX_MUTATION_ROWS) in result["error"]


def test_where_first_clause_cannot_use_or(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])

    result = server.count_rows(
        "DeviceBaseInfo",
        where=[{"logic": "OR", "field": "DeviceID", "op": "=", "value": 1}],
        connection=DEMO,
    )

    assert result["ok"] is False
    assert "first where clause" in result["error"]


def test_where_special_characters_are_rejected_by_default(fake_db):
    fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])

    result = server.count_rows(
        "DeviceBaseInfo",
        where=[{"field": "note", "op": "=", "value": "a--b"}],
        connection=DEMO,
    )

    assert result["ok"] is False
    assert "forbidden SQL syntax" in result["error"]


def test_or_between_clauses_is_honoured():
    sql, params = server._build_where_clause(
        [
            {"field": "a", "op": "=", "value": 1},
            {"logic": "OR", "field": "b", "op": "=", "value": 2},
        ],
        None,
    )

    assert sql == "`a` = %s OR `b` = %s"
    assert params == [1, 2]


def test_in_operator_binds_each_value():
    sql, params = server._build_where_clause(
        [{"field": "a", "op": "IN", "value": [1, 2, 3]}], None
    )

    assert sql == "`a` IN (%s, %s, %s)"
    assert params == [1, 2, 3]


def test_raw_sql_in_where_is_rejected():
    with pytest.raises(TypeError):
        server._build_where_clause("a = 1", None)


def test_where_params_mismatch_is_rejected():
    with pytest.raises(ValueError):
        server._build_where_clause([{"field": "a", "op": "=", "value": 1}], [1, 2])


def test_query_timeout_is_applied_by_default(fake_db):
    cursor = fake_db([(matches("SELECT COUNT(*)"), [{"total": 0}])])

    server.count_rows("DeviceBaseInfo", connection=DEMO)

    assert any(
        sql.startswith("SET SESSION MAX_EXECUTION_TIME") for sql in executed_sql(cursor)
    )


def test_connection_config_has_a_default_timeout(demo_profile):
    demo_profile("bare", host="demo-host")

    config = server.get_connection_config("bare")

    assert config["connection_timeout"] == server.DEFAULT_CONNECT_TIMEOUT


def test_connection_config_reads_the_profile_port(demo_profile):
    demo_profile("bare", host="demo-host", port=3307)

    config = server.get_connection_config("bare")

    assert config["port"] == 3307
    assert config["host"] == "demo-host"


def test_connection_config_omits_ssl_options_when_unset(demo_profile):
    demo_profile("bare", host="demo-host")

    config = server.get_connection_config("bare")

    assert not any(key.startswith("ssl_") for key in config)


def test_connection_config_passes_ssl_options_through(demo_profile):
    demo_profile(
        "bare",
        host="demo-host",
        ssl_ca="/etc/mysql/ca.pem",
        ssl_verify_cert=True,
    )

    config = server.get_connection_config("bare")

    assert config["ssl_ca"] == "/etc/mysql/ca.pem"
    assert config["ssl_verify_cert"] is True


def test_a_call_without_a_connection_is_refused(fake_db):
    """There is no environment default any more: a bare call must not dial anything."""
    cursor = fake_db([(matches("SHOW TABLES"), [])])

    result = server.list_tables()

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
    assert "connection" in result["error"]
    assert cursor.executed == []


def test_identifier_accepts_plain_names():
    assert server._safe_identifier("users", "table_name") == "users"
    assert server._safe_identifier("  t_1  ", "table_name") == "t_1"


@pytest.mark.parametrize("name", ["db.users", "订单表", "", "1users", "user$info"])
def test_identifier_rejects_unsupported_names(name):
    with pytest.raises(ValueError):
        server._safe_identifier(name, "table_name")


def test_identifier_rejects_non_string():
    with pytest.raises(TypeError):
        server._safe_identifier(123, "table_name")


def test_execute_sql_alias_still_works(fake_db):
    fake_db([(matches("SELECT"), [{"n": 1}])])

    result = server.execute_sql("SELECT 1", connection=DEMO)

    assert result["ok"] is True


def test_execute_sql_is_not_exposed_as_a_duplicate_tool():
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert "execute_query" in names
    assert "execute_sql" not in names
    assert len(names) == 13


def test_connection_management_tools_are_exposed():
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert {"list_connections", "save_connection", "delete_connection"} <= names


def test_a_profile_without_a_database_omits_it(demo_profile):
    demo_profile("bare", host="demo-host", database=None)

    config = server.get_connection_config("bare")

    assert "database" not in config
    assert config["host"] == "demo-host"


def test_every_tool_accepts_a_connection_argument():
    tools = asyncio.run(server.mcp.list_tools())
    connection_tools = {
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
        if tool.name in connection_tools:
            assert "connection" in tool.input_schema["properties"], tool.name


def test_unknown_connection_reports_the_available_names(fake_db):
    fake_db([(matches("SELECT"), [{"n": 1}])])

    result = server.execute_query("SELECT 1", connection="nope")

    assert result["ok"] is False
    assert result["code"] == "CONNECTION_ERROR"
    assert "nope" in result["error"]


def test_every_registered_tool_has_a_description():
    tools = asyncio.run(server.mcp.list_tools())

    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_no_tool_mentions_the_environment_as_a_connection_source():
    """The env-default connection is gone; the docs must not promise one."""
    tools = asyncio.run(server.mcp.list_tools())

    for tool in tools:
        text = f"{tool.description} {tool.input_schema}"
        assert "MYSQL_" not in text, tool.name
        assert "server environment" not in text, tool.name


def test_connections_module_reads_no_connection_env():
    """Only the file location may come from the environment."""
    import inspect

    source = inspect.getsource(connections)
    assert "MYSQL_ALLOWED_HOSTS" not in source
    assert not hasattr(connections, "allowed_hosts")
    assert not hasattr(connections, "assert_host_allowed")
