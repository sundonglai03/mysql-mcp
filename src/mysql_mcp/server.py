"""MySQL MCP server.

Exposed over stdio. Everything is conservative by default: read-only SQL, a hard
cap on result-set size, structured (never raw) WHERE clauses, a cap on how many
rows a single mutation may touch, connection/statement timeouts and TLS support.

Several databases are supported without restarting the process: every tool takes
the complete inline ``credentials`` for that operation. Credentials are never
persisted or looked up by the MCP server.
"""

import argparse
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mysql.connector import Error as MySQLError
from pydantic import Field

from . import credentials as credential_config
from .client import MySQLClient
from .credentials import CredentialError
from .http_auth import BearerTokenMiddleware, HealthEndpointMiddleware

mcp = MCPServer("mysql-mcp")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_QUERY_LIMIT = 50
MAX_QUERY_LIMIT = 500
DEFAULT_MAX_MUTATION_ROWS = 1000
MAX_MUTATION_ROWS = 10000
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_QUERY_TIMEOUT_MS = 30_000

_ALLOWED_BOOLEAN_OPS = ("AND", "OR")
_ALLOWED_COMP_OPS = (
    "=",
    "!=",
    "<>",
    ">",
    ">=",
    "<",
    "<=",
    "LIKE",
    "IN",
    "IS",
    "IS NOT",
)

_READ_ONLY_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")

# Clauses that look read-only but are not: they write files on the server or take
# locks, and `EXPLAIN ANALYZE` actually *executes* the statement it is given.
_FORBIDDEN_READ_ONLY_CLAUSES = (
    (
        re.compile(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b"),
        "SELECT ... INTO OUTFILE/DUMPFILE",
    ),
    (
        re.compile(r"\bEXPLAIN\s+ANALYZE\b"),
        "EXPLAIN ANALYZE (it executes the statement)",
    ),
    (re.compile(r"\bFOR\s+UPDATE\b"), "FOR UPDATE"),
    (re.compile(r"\bLOCK\s+IN\s+SHARE\s+MODE\b"), "LOCK IN SHARE MODE"),
)

_FORBIDDEN_VALUE_TOKENS = (";", "--", "/*", "*/")

_WHERE_DOC = (
    "`where` must be structured, never raw SQL: a dict, or a list of dicts, each with "
    "`field` (column name), `op` (one of =, !=, <>, >, >=, <, <=, LIKE, IN, IS, IS NOT), "
    "`value`, and an optional `logic` (AND or OR, only meaningful from the second clause on). "
    'Example: [{"field": "status", "op": "=", "value": "active"}, '
    '{"logic": "AND", "field": "age", "op": ">", "value": 18}].'
)

_CONNECTION_DOC = (
    "Pass the complete database login in `credentials` on every operation. "
    "The server uses it only for this call and never reads or writes saved profiles."
)

Credentials = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Complete login details for this operation. Shape: "
            '{"host": "10.0.0.5", "port": 3306, "user": "root", "password": "...", '
            '"database": "mydb", "charset": "utf8mb4"} — `host` is required. '
            "Credentials are discarded after the operation."
        )
    ),
]


@dataclass(frozen=True)
class ConnectionInfo:
    """Validated driver arguments and per-call safety policy."""

    config: dict[str, Any] = field(repr=False)
    read_only: bool = False
    max_affected_rows: int | None = None
    database: str | None = None


class ReadOnlyConnectionError(ValueError):
    """A write was attempted with read-only credentials."""


def _credential_ssl_options(credential_values: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in ("ssl_ca", "ssl_cert", "ssl_key", "ssl_cipher"):
        if credential_values.get(key):
            options[key] = credential_values[key]
    for key in ("ssl_disabled", "ssl_verify_cert", "ssl_verify_identity"):
        if key in credential_values:
            options[key] = credential_values[key]
    return options


def _connection_config(credential_values: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "host": credential_values["host"],
        "port": credential_values.get("port", credential_config.DEFAULT_PORT),
        "user": credential_values.get("user", "root"),
        "password": credential_values.get("password", ""),
        "charset": credential_values.get("charset", credential_config.DEFAULT_CHARSET),
        "autocommit": True,
    }
    if credential_values.get("database"):
        config["database"] = credential_values["database"]
    config.update(_credential_ssl_options(credential_values))
    return config


def resolve_request(credentials: Credentials) -> ConnectionInfo:
    """Resolve one stateless tool call from credentials supplied by the caller.

    Credentials are required for every call and are used only for that call.
    """
    credential_values = credential_config.normalize(credentials)

    config = _connection_config(credential_values)
    config["connection_timeout"] = DEFAULT_CONNECT_TIMEOUT
    return ConnectionInfo(
        config=config,
        read_only=bool(credential_values.get("read_only", False)),
        max_affected_rows=credential_values.get("max_affected_rows"),
        database=config.get("database"),
    )


def _dial(config: dict[str, Any]):
    """Single seam through which every connection is opened (tests patch this)."""
    return MySQLClient(config).connect()


@contextmanager
def _connection(
    credentials: Credentials,
    *,
    write_operation: str | None = None,
) -> Iterator[tuple[Any, ConnectionInfo]]:
    """Validate credentials, refuse disallowed writes, then dial.

    The policy check deliberately runs before connecting, so a read-only request
    reports ``CONNECTION_READ_ONLY`` even when the host is unreachable.
    """
    info = resolve_request(credentials)
    if write_operation is not None:
        _assert_writable(info, write_operation)
    conn = _dial(info.config)

    try:
        yield conn, info
    finally:
        _close_quietly(conn)


def _close_quietly(conn: Any) -> None:
    MySQLClient._close_raw(conn)


def _connection_fields(info: ConnectionInfo) -> dict[str, Any]:
    """Return a password-free description of the target used for this call."""
    return {
        "target": {
            "host": info.config["host"],
            "port": info.config["port"],
            "database": info.database,
        }
    }


def _require_database(info: ConnectionInfo) -> str:
    if not info.database:
        raise ValueError(
            "credentials has no database selected; include the 'database' field"
        )
    return info.database


def _assert_writable(info: ConnectionInfo, operation: str) -> None:
    if info.read_only:
        raise ReadOnlyConnectionError(
            f"credentials is read-only, so {operation} is refused"
        )


def _effective_mutation_cap(requested: Any, info: ConnectionInfo) -> int:
    cap = _resolve_mutation_cap(requested)
    if info.max_affected_rows is not None:
        cap = min(cap, info.max_affected_rows)
    return cap


def _apply_query_timeout(conn: Any) -> None:
    """Best-effort server-side cap on read query runtime.

    MySQL only; MariaDB uses a different variable, so failures are swallowed and
    the connection is used as-is.
    """
    MySQLClient.apply_query_timeout_to(conn, DEFAULT_QUERY_TIMEOUT_MS)


@contextmanager
def _read_connection(
    credentials: Credentials,
    *,
    write_operation: str | None = None,
) -> Iterator[tuple[Any, ConnectionInfo]]:
    with _connection(credentials, write_operation=write_operation) as (
        conn,
        info,
    ):
        _apply_query_timeout(conn)
        yield conn, info


def _safe_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must be a non-empty string")
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise ValueError(f"{field_name} must be a valid SQL identifier: {value!r}")
    return candidate


def _coerce_positive_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _resolve_limit(limit: Any) -> tuple[int, bool]:
    """Return ``(applied_limit, truncated)`` for a caller-supplied row cap."""
    requested = _coerce_positive_int(limit, "limit", DEFAULT_QUERY_LIMIT)
    applied = min(requested, MAX_QUERY_LIMIT)
    return applied, requested > applied


def _resolve_mutation_cap(value: Any) -> int:
    cap = _coerce_positive_int(value, "max_affected_rows", DEFAULT_MAX_MUTATION_ROWS)
    if cap > MAX_MUTATION_ROWS:
        raise ValueError(f"max_affected_rows cannot exceed {MAX_MUTATION_ROWS}")
    return cap


def _strip_trailing_semicolon(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().rstrip(";").strip()


def _error_result(message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return payload


def _mask_literals_and_comments(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments.

    Structural keywords can then be matched without false positives (a `--` or
    `;` inside a value is not syntax) and without false negatives (a `#` comment
    cannot hide a trailing clause).
    """
    out: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char == "#" or (
            char == "-"
            and index + 1 < length
            and sql[index + 1] == "-"
            and (index + 2 >= length or sql[index + 2].isspace())
        ):
            while index < length and sql[index] != "\n":
                out.append(" ")
                index += 1
            continue

        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            while index < length and not (
                sql[index] == "*" and index + 1 < length and sql[index + 1] == "/"
            ):
                out.append(" ")
                index += 1
            out.append(" ")
            out.append(" ")
            index += 2
            continue

        if char in ("'", '"', "`"):
            quote = char
            out.append(" ")
            index += 1
            while index < length:
                current = sql[index]
                if current == "\\" and quote != "`":
                    out.append(" ")
                    index += 1
                    if index < length:
                        out.append(" ")
                        index += 1
                    continue
                if current == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        out.append(" ")
                        out.append(" ")
                        index += 2
                        continue
                    out.append(" ")
                    index += 1
                    break
                out.append(" ")
                index += 1
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _contains_multiple_statements(sql: str) -> bool:
    return ";" in _mask_literals_and_comments(sql)


def _assert_statement_allowed(statement: str, read_only: bool) -> None:
    if not read_only:
        return

    masked = _mask_literals_and_comments(statement).strip().upper()
    if not masked.startswith(_READ_ONLY_PREFIXES):
        raise ValueError(
            "read_only mode only allows SELECT/SHOW/DESCRIBE/DESC/EXPLAIN statements; "
            "pass read_only=false to run anything else"
        )
    for pattern, label in _FORBIDDEN_READ_ONLY_CLAUSES:
        if pattern.search(masked):
            raise ValueError(
                f"read_only mode forbids {label}; pass read_only=false if you really need it"
            )


def _check_value_tokens(value: Any) -> None:
    """Reject literal values that look like smuggled SQL.

    Bound parameters are already injection-safe; this is belt-and-braces for the
    common case where a filter value is a plain identifier-ish string anyway.
    """
    if isinstance(value, str) and any(
        token in value for token in _FORBIDDEN_VALUE_TOKENS
    ):
        raise ValueError(
            "where value contains forbidden SQL syntax (';', '--', '/*' or '*/')"
        )


def _normalize_where_value(raw_value: Any) -> list[Any]:
    if raw_value is None:
        return [None]
    if isinstance(raw_value, (list, tuple, set)):
        values = list(raw_value)
        if not values:
            raise ValueError("where value list cannot be empty")
        for value in values:
            _check_value_tokens(value)
        return values
    _check_value_tokens(raw_value)
    return [raw_value]


def _normalize_order_entries(order_by: Any) -> list[dict[str, str]]:
    if order_by is None:
        return []
    if isinstance(order_by, str):
        items: list[Any] = [_strip_trailing_semicolon(order_by)]
    elif isinstance(order_by, list):
        items = list(order_by)
    else:
        raise TypeError(
            "order_by must be a string, a list of field names, or a list of dicts"
        )

    normalized: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            fragment = _strip_trailing_semicolon(item)
            if not fragment:
                continue
            match = re.fullmatch(
                r"`?(?P<field>[A-Za-z_][A-Za-z0-9_]*)`?\s*(?P<direction>ASC|DESC)?",
                fragment,
                re.IGNORECASE,
            )
            if not match:
                raise ValueError(
                    "order_by entries must be simple column names with optional ASC/DESC"
                )
            direction = (match.group("direction") or "ASC").upper()
            normalized.append({"field": match.group("field"), "direction": direction})
        elif isinstance(item, dict):
            field = item.get("field")
            direction = str(item.get("direction", "ASC")).upper()
            if direction not in {"ASC", "DESC"}:
                raise ValueError("order_by direction must be ASC or DESC")
            normalized.append(
                {
                    "field": _safe_identifier(field, "order_by field"),
                    "direction": direction,
                }
            )
        else:
            raise TypeError("order_by entries must be strings or dicts")
    return normalized


def _build_where_clause(
    where: Any, where_params: list[Any] | None
) -> tuple[str, list[Any]]:
    if where is None:
        return "", []
    if isinstance(where, str):
        raise TypeError(
            "where must be a structured condition list or dict, not raw SQL text"
        )
    if isinstance(where, dict):
        clauses: list[Any] = [where]
    elif isinstance(where, list):
        clauses = list(where)
    else:
        raise TypeError("where must be a dict or a list of dicts")

    parts: list[str] = []
    bound_params: list[Any] = []
    provided_params = list(where_params or [])

    for index, raw_clause in enumerate(clauses):
        if not isinstance(raw_clause, dict):
            raise TypeError("each where clause must be a dict")
        logic = str(raw_clause.get("logic", "AND")).upper()
        if logic not in _ALLOWED_BOOLEAN_OPS:
            raise ValueError("where logic must be AND or OR")
        if index == 0:
            if logic != "AND":
                raise ValueError(
                    "the first where clause cannot use OR; pass one clause per condition and "
                    "set 'logic' only on the second clause onward"
                )
        else:
            parts.append(logic)

        field = raw_clause.get("field")
        op = str(raw_clause.get("op", "=")).upper()
        if op not in _ALLOWED_COMP_OPS:
            raise ValueError(f"unsupported where operator: {op!r}")
        safe_field = _safe_identifier(field, "where field")
        raw_value = raw_clause.get("value")

        if isinstance(raw_value, (list, tuple, set)):
            raw_value = _normalize_where_value(raw_value)
        else:
            _check_value_tokens(raw_value)

        if op == "IN":
            values = _normalize_where_value(raw_value)
            if len(values) == 1 and isinstance(values[0], (list, tuple, set)):
                values = list(values[0])
            if not values:
                raise ValueError("IN conditions require at least one value")
            placeholders = ", ".join(["%s"] * len(values))
            parts.append(f"`{safe_field}` IN ({placeholders})")
            bound_params.extend(values)
        elif op in {"IS", "IS NOT"}:
            if raw_value is None or (
                isinstance(raw_value, str) and raw_value.upper() == "NULL"
            ):
                parts.append(f"`{safe_field}` {op} NULL")
            else:
                parts.append(f"`{safe_field}` {op} %s")
                bound_params.append(raw_value)
        else:
            if raw_value is None and op != "=":
                raise ValueError("NULL comparisons require '=', 'IS' or 'IS NOT'")
            parts.append(f"`{safe_field}` {op} %s")
            bound_params.append(raw_value)

    if provided_params:
        if len(provided_params) != len(bound_params):
            raise ValueError(
                "where_params count does not match the structured where clauses"
            )
        bound_params = list(provided_params)
    return " ".join(parts), bound_params


def _validate_order_by(order_by: Any) -> str:
    if order_by is None:
        return ""
    entries = _normalize_order_entries(order_by)
    if not entries:
        raise ValueError("order_by cannot be empty")
    return ", ".join(f"`{entry['field']}` {entry['direction']}" for entry in entries)


def _get_table_columns(conn: Any, database: str, table_name: str) -> list[str]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME AS column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database, table_name),
        )
        rows = cursor.fetchall()
    finally:
        try:
            cursor.close()
        except MySQLError:
            pass
    columns: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("column_name") is not None:
            columns.append(str(row["column_name"]))
    return columns


def _count_matching_rows(
    conn: Any, table: str, where_clause: str, bindings: list[Any]
) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM `{table}` WHERE {where_clause}", bindings)
        row = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except MySQLError:
            pass
    if isinstance(row, (tuple, list)):
        return int(row[0]) if row else 0
    return int(row) if row is not None else 0


def _mutation_cap_error(table: str, matched: int, cap: int) -> dict[str, Any]:
    return _error_result(
        f"Refusing to modify {matched} rows in `{table}`: this exceeds max_affected_rows={cap}. "
        f"Narrow the WHERE clause, or pass a larger max_affected_rows (hard cap {MAX_MUTATION_ROWS}).",
        table=table,
        matched_rows=matched,
        max_affected_rows=cap,
        code="MUTATION_LIMIT_EXCEEDED",
    )


@mcp.tool(
    description=(
        "Check whether a MySQL connection is reachable and the configured database is usable. "
        f"{_CONNECTION_DOC}"
    )
)
def health_check(
    credentials: Credentials,
) -> dict[str, Any]:
    try:
        with _read_connection(credentials) as (conn, info):
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT DATABASE() AS database_name, VERSION() AS server_version"
            )
            row = cursor.fetchone() or {}
            return {
                "ok": True,
                **_connection_fields(info),
                "host": info.config.get("host"),
                "read_only": info.read_only,
                "database": row.get("database_name"),
                "server_version": row.get("server_version"),
            }
    except MySQLError as exc:
        return _error_result(str(exc), code="CONNECTION_FAILED")
    except CredentialError as exc:
        return _error_result(str(exc), code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), code="CONFIG_ERROR")


@mcp.tool(
    description=(
        f"List the names of all tables in the current database. {_CONNECTION_DOC}"
    )
)
def list_tables(
    credentials: Credentials,
) -> list[str] | dict[str, Any]:
    try:
        with _connection(credentials) as (conn, info):
            _require_database(info)
            cursor = conn.cursor()
            cursor.execute("SHOW TABLES")
            rows = cursor.fetchall()
            return [
                str(row[0]) if isinstance(row, (tuple, list)) else str(row)
                for row in rows
            ]
    except MySQLError as exc:
        return _error_result(f"Failed to list tables: {exc}", code="QUERY_FAILED")
    except CredentialError as exc:
        return _error_result(str(exc), code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), code="CONFIG_ERROR")


@mcp.tool(
    description=(
        "List the names of all databases available to the current MySQL user. "
        f"{_CONNECTION_DOC}"
    )
)
def get_databases(
    credentials: Credentials,
) -> list[str] | dict[str, Any]:
    try:
        with _connection(credentials) as (conn, _info):
            cursor = conn.cursor()
            cursor.execute("SHOW DATABASES")
            rows = cursor.fetchall()
            return [
                str(row[0]) if isinstance(row, (tuple, list)) else str(row)
                for row in rows
            ]
    except MySQLError as exc:
        return _error_result(f"Failed to list databases: {exc}", code="QUERY_FAILED")
    except CredentialError as exc:
        return _error_result(str(exc), code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), code="CONFIG_ERROR")


@mcp.tool(
    description=(
        "Fetch a bounded number of rows from a table, with optional filtering and sorting. "
        f"{_WHERE_DOC} `order_by` takes column names, optionally with ASC/DESC, either as a "
        'string or a list such as [{"field": "created_at", "direction": "DESC"}]. '
        f"{_CONNECTION_DOC}"
    )
)
def fetch_table(
    table_name: str,
    credentials: Credentials,
    limit: int = DEFAULT_QUERY_LIMIT,
    where: dict[str, Any] | list[dict[str, Any]] | None = None,
    order_by: str | list[str | dict[str, str]] | None = None,
    where_params: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        safe_table = _safe_identifier(table_name, "table_name")
        limit_value, truncated = _resolve_limit(limit)
        where_clause, where_bindings = (
            _build_where_clause(where, where_params) if where else ("", [])
        )
        order_clause = _validate_order_by(order_by) if order_by else ""

        sql = f"SELECT * FROM `{safe_table}`"
        params: list[Any] = []
        if where_clause:
            sql += f" WHERE {where_clause}"
            params.extend(where_bindings)
        if order_clause:
            sql += f" ORDER BY {order_clause}"
        sql += " LIMIT %s"
        params.append(limit_value)

        with _read_connection(credentials) as (conn, info):
            database = _require_database(info)
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            if rows and isinstance(rows[0], dict):
                columns = list(rows[0].keys())
            else:
                columns = _get_table_columns(conn, database, safe_table)

            payload: dict[str, Any] = {
                "ok": True,
                **_connection_fields(info),
                "table": safe_table,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "limit_applied": limit_value,
                "truncated": truncated,
            }
            if truncated:
                payload["warning"] = (
                    f"Result set truncated to {limit_value} rows by the server safety cap."
                )
            return payload
    except MySQLError as exc:
        return _error_result(
            f"Failed to query table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        f"Count rows in a table, optionally with a filter. {_WHERE_DOC} {_CONNECTION_DOC}"
    )
)
def count_rows(
    table_name: str,
    credentials: Credentials,
    where: dict[str, Any] | list[dict[str, Any]] | None = None,
    where_params: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        safe_table = _safe_identifier(table_name, "table_name")
        sql = f"SELECT COUNT(*) AS total FROM `{safe_table}`"
        params: list[Any] = []
        if where:
            where_clause, where_bindings = _build_where_clause(where, where_params)
            sql += f" WHERE {where_clause}"
            params.extend(where_bindings)

        with _read_connection(credentials) as (conn, info):
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql, params)
            result = cursor.fetchone()
            if isinstance(result, dict):
                total = result.get("total", 0)
            elif isinstance(result, (tuple, list)):
                total = result[0] if result else 0
            else:
                total = 0
            return {
                "ok": True,
                **_connection_fields(info),
                "table": safe_table,
                "total": total,
            }
    except MySQLError as exc:
        return _error_result(
            f"Failed to count rows in table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        f"Insert a single row into a table using column/value pairs. {_CONNECTION_DOC}"
    )
)
def insert_row(
    table_name: str,
    row: dict[str, Any],
    credentials: Credentials,
) -> dict[str, Any]:
    try:
        if not isinstance(row, dict):
            raise TypeError("row must be a dict of column/value pairs")
        if not row:
            raise ValueError("row cannot be empty")

        safe_table = _safe_identifier(table_name, "table_name")
        columns = [_safe_identifier(column_name, "column_name") for column_name in row]
        placeholders = ", ".join(["%s"] * len(columns))
        column_sql = ", ".join(f"`{column}`" for column in columns)
        sql = f"INSERT INTO `{safe_table}` ({column_sql}) VALUES ({placeholders})"

        with _connection(credentials, write_operation="insert_row") as (
            conn,
            info,
        ):
            cursor = conn.cursor()
            cursor.execute(sql, list(row.values()))
            conn.commit()
            return {
                "ok": True,
                **_connection_fields(info),
                "table": safe_table,
                "inserted": cursor.rowcount,
                "columns": columns,
            }
    except MySQLError as exc:
        return _error_result(
            f"Failed to insert into table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except ReadOnlyConnectionError as exc:
        return _error_result(
            str(exc), table_name=table_name, code="CONNECTION_READ_ONLY"
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        "Update rows in a table. Requires an explicit filter, and refuses to touch more than "
        f"max_affected_rows rows. {_WHERE_DOC} {_CONNECTION_DOC}"
    )
)
def update_rows(
    table_name: str,
    updates: dict[str, Any],
    where: dict[str, Any] | list[dict[str, Any]],
    credentials: Credentials,
    where_params: list[Any] | None = None,
    max_affected_rows: int = DEFAULT_MAX_MUTATION_ROWS,
) -> dict[str, Any]:
    try:
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict of column/value pairs")
        if not updates:
            raise ValueError("updates cannot be empty")
        if not where:
            raise ValueError("where cannot be empty")

        safe_table = _safe_identifier(table_name, "table_name")
        where_clause, where_bindings = _build_where_clause(where, where_params)
        set_clause = ", ".join(
            f"`{_safe_identifier(str(column), 'column_name')}` = %s"
            for column in updates
        )
        params = list(updates.values()) + where_bindings
        sql = f"UPDATE `{safe_table}` SET {set_clause} WHERE {where_clause}"

        with _connection(credentials, write_operation="update_rows") as (
            conn,
            info,
        ):
            row_cap = _effective_mutation_cap(max_affected_rows, info)
            matched = _count_matching_rows(
                conn, safe_table, where_clause, where_bindings
            )
            if matched > row_cap:
                return _mutation_cap_error(safe_table, matched, row_cap)
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            return {
                "ok": True,
                **_connection_fields(info),
                "table": safe_table,
                "matched": matched,
                "updated": cursor.rowcount,
                "max_affected_rows": row_cap,
            }
    except MySQLError as exc:
        return _error_result(
            f"Failed to update table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except ReadOnlyConnectionError as exc:
        return _error_result(
            str(exc), table_name=table_name, code="CONNECTION_READ_ONLY"
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        "Delete rows from a table. Requires an explicit filter, and refuses to delete more than "
        f"max_affected_rows rows. {_WHERE_DOC} {_CONNECTION_DOC}"
    )
)
def delete_rows(
    table_name: str,
    where: dict[str, Any] | list[dict[str, Any]],
    credentials: Credentials,
    where_params: list[Any] | None = None,
    max_affected_rows: int = DEFAULT_MAX_MUTATION_ROWS,
) -> dict[str, Any]:
    try:
        if not where:
            raise ValueError("where cannot be empty")

        safe_table = _safe_identifier(table_name, "table_name")
        where_clause, where_bindings = _build_where_clause(where, where_params)
        sql = f"DELETE FROM `{safe_table}` WHERE {where_clause}"

        with _connection(credentials, write_operation="delete_rows") as (
            conn,
            info,
        ):
            row_cap = _effective_mutation_cap(max_affected_rows, info)
            matched = _count_matching_rows(
                conn, safe_table, where_clause, where_bindings
            )
            if matched > row_cap:
                return _mutation_cap_error(safe_table, matched, row_cap)
            cursor = conn.cursor()
            cursor.execute(sql, where_bindings)
            conn.commit()
            return {
                "ok": True,
                **_connection_fields(info),
                "table": safe_table,
                "matched": matched,
                "deleted": cursor.rowcount,
                "max_affected_rows": row_cap,
            }
    except MySQLError as exc:
        return _error_result(
            f"Failed to delete from table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except ReadOnlyConnectionError as exc:
        return _error_result(
            str(exc), table_name=table_name, code="CONNECTION_READ_ONLY"
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        "Describe the columns and schema metadata for a specific table in the current database. "
        f"{_CONNECTION_DOC}"
    )
)
def describe_table(
    table_name: str,
    credentials: Credentials,
) -> list[dict[str, Any]] | dict[str, Any]:
    try:
        safe_table = _safe_identifier(table_name, "table_name")
        with _read_connection(credentials) as (conn, info):
            database = _require_database(info)
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT COLUMN_NAME AS column_name,
                       DATA_TYPE AS data_type,
                       IS_NULLABLE AS is_nullable,
                       COLUMN_DEFAULT AS column_default,
                       EXTRA AS extra
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ORDINAL_POSITION
                """,
                (database, safe_table),
            )
            rows = cursor.fetchall()
            if not rows:
                return _error_result(
                    f"Table {safe_table!r} does not exist in database {database!r}.",
                    table=safe_table,
                    database=database,
                )
            return [dict(row) for row in rows if isinstance(row, dict)]
    except MySQLError as exc:
        return _error_result(
            f"Failed to describe table {table_name!r}: {exc}",
            table=table_name,
            code="QUERY_FAILED",
        )
    except CredentialError as exc:
        return _error_result(str(exc), table_name=table_name, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), table_name=table_name)


@mcp.tool(
    description=(
        "Execute a single SQL statement, read-only by default: only "
        "SELECT/SHOW/DESCRIBE/DESC/EXPLAIN are accepted, and clauses that write files on the "
        "server or take locks (INTO OUTFILE/DUMPFILE, FOR UPDATE, EXPLAIN ANALYZE) are rejected. "
        "Multi-statement SQL is never allowed. Pass read_only=false to run writes. Result sets "
        f"are capped at `limit` rows. {_CONNECTION_DOC}"
    )
)
def execute_query(
    sql: str,
    credentials: Credentials,
    limit: int = DEFAULT_QUERY_LIMIT,
    read_only: bool = True,
) -> dict[str, Any]:
    try:
        statement = _strip_trailing_semicolon(sql)
        if not statement:
            raise ValueError("sql cannot be empty")
        if _contains_multiple_statements(statement):
            raise ValueError(
                "single statement only: multi-statement SQL is not allowed"
            )
        _assert_statement_allowed(statement, read_only)
        limit_value, truncated = _resolve_limit(limit)

        write_operation = None if read_only else "execute_query(read_only=false)"
        with _read_connection(
            credentials,
            write_operation=write_operation,
        ) as (
            conn,
            info,
        ):
            cursor = conn.cursor(dictionary=True)
            cursor.execute(statement)

            if cursor.description:
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(limit_value)
                payload: dict[str, Any] = {
                    "ok": True,
                    **_connection_fields(info),
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "read_only": read_only,
                    "limit_applied": limit_value,
                    "truncated": truncated,
                }
                if truncated:
                    payload["warning"] = (
                        f"Result set truncated to {limit_value} rows by the server safety cap."
                    )
                return payload

            return {
                "ok": True,
                **_connection_fields(info),
                "rows_affected": cursor.rowcount,
                "read_only": read_only,
            }
    except MySQLError as exc:
        return _error_result(
            f"Query execution failed: {exc}", sql=sql, code="QUERY_FAILED"
        )
    except ReadOnlyConnectionError as exc:
        return _error_result(str(exc), sql=sql, code="CONNECTION_READ_ONLY")
    except CredentialError as exc:
        return _error_result(str(exc), sql=sql, code="CONNECTION_ERROR")
    except (ValueError, TypeError) as exc:
        return _error_result(str(exc), sql=sql)


def _streamable_http_path(value: str) -> str:
    """Validate the endpoint path accepted by the MCP SDK."""
    if not value.startswith("/"):
        raise argparse.ArgumentTypeError("must start with '/'")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def main(argv: list[str] | None = None) -> None:
    """Start the server over stdio (default) or Streamable HTTP."""
    parser = argparse.ArgumentParser(description="MySQL MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=_port, default=8000, help="HTTP bind port")
    parser.add_argument(
        "--path",
        type=_streamable_http_path,
        default="/mcp",
        help="Streamable HTTP endpoint path",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="return JSON responses instead of streaming SSE responses",
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        help="do not retain MCP sessions on the server",
    )
    parser.add_argument(
        "--auth-token",
        default=os.getenv("MYSQL_MCP_AUTH_TOKEN"),
        help="optional Bearer token; can also be set with MYSQL_MCP_AUTH_TOKEN",
    )
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    _run_http(args)


def create_http_app(
    *,
    host: str,
    path: str,
    json_response: bool,
    stateless_http: bool,
    token: str | None,
):
    app = mcp.streamable_http_app(
        streamable_http_path=path,
        json_response=json_response,
        stateless_http=stateless_http,
        host=host,
    )
    app = HealthEndpointMiddleware(app)
    return BearerTokenMiddleware(app, token) if token else app


def _run_http(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        create_http_app(
            host=args.host,
            path=args.path,
            json_response=args.json_response,
            stateless_http=args.stateless_http,
            token=args.auth_token,
        ),
        host=args.host,
        port=args.port,
        log_level=mcp.settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
