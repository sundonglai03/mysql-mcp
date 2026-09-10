"""Shared test doubles for the mysql-mcp suite."""

from __future__ import annotations

from typing import Any


def matches(prefix: str):
    """Build a predicate matching statements that start with ``prefix``."""

    def predicate(sql: str) -> bool:
        return sql.lstrip().upper().startswith(prefix.upper())

    return predicate


class FakeCursor:
    """Scriptable cursor: ``responses`` is a list of (predicate, value) pairs."""

    def __init__(self, responses: list[tuple[Any, Any]] | None = None):
        self._responses = list(responses or [])
        self.executed: list[tuple[str, Any]] = []
        self.description = None
        self.rowcount = 0
        self._response: Any = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.description = None
        self._response = []
        for predicate, value in self._responses:
            if predicate(sql):
                self._response = value
                break
        if isinstance(self._response, list):
            self.rowcount = len(self._response)
            first = self._response[0] if self._response else None
            if isinstance(first, dict):
                self.description = tuple(
                    (key, None, None, None, None, None, None) for key in first
                )
            elif first is not None:
                self.description = (("col", None, None, None, None, None, None),)
        else:
            self.rowcount = 1
        return self

    def fetchall(self):
        return self._response if isinstance(self._response, list) else []

    def fetchmany(self, size=None):
        rows = self.fetchall()
        return rows if size is None else rows[:size]

    def fetchone(self):
        if isinstance(self._response, list):
            return self._response[0] if self._response else None
        return self._response

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor, database: str = "demo"):
        self._cursor = cursor
        self.database = database

    def cursor(self, dictionary: bool = False):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


def executed_sql(cursor: FakeCursor) -> list[str]:
    return [sql for sql, _ in cursor.executed]
