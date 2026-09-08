"""Tests for SqlRunner (sql.py). Uses a minimal fake DB-API-shaped
connection/cursor — SqlRunner only ever calls .cursor(), .execute(),
.description, .fetchall(), .rowcount, .close() (plus .commit()/.rollback()/
.autocommit() for run_transaction()), so that's all the fake needs to
provide.
"""

from __future__ import annotations

import pytest

from snowrig.sql import SqlRunner


class _FakeCursor:
    def __init__(self, *, description=None, rows=None, rowcount=0, raise_on=None):
        self.description = description
        self._rows = rows or []
        self.rowcount = rowcount
        self.executed: list[str] = []
        self.closed = False
        self._raise_on = raise_on or {}

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql in self._raise_on:
            raise self._raise_on[sql]

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.autocommit_calls: list[bool] = []

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def autocommit(self, value):
        self.autocommit_calls.append(value)


# --------------------------------------------------------------------- #
# run_query — the method api/server.py now delegates to
# --------------------------------------------------------------------- #

def test_run_query_returns_columns_rows_rowcount():
    cursor = _FakeCursor(description=[("ID",), ("NAME",)], rows=[(1, "Alice")], rowcount=1)
    runner = SqlRunner(_FakeConnection(cursor))

    columns, rows, rowcount = runner.run_query("SELECT id, name FROM customers")

    assert columns == ["ID", "NAME"]
    assert rows == [(1, "Alice")]
    assert rowcount == 1


def test_run_query_no_result_set_returns_empty_columns_and_rows():
    """DDL/DML statements have no cursor.description — must not crash
    trying to read columns off a None description."""
    cursor = _FakeCursor(description=None, rowcount=3)
    runner = SqlRunner(_FakeConnection(cursor))

    columns, rows, rowcount = runner.run_query("UPDATE customers SET active = TRUE")

    assert columns == []
    assert rows == []
    assert rowcount == 3


def test_run_query_always_closes_cursor_on_success():
    cursor = _FakeCursor(description=[("X",)], rows=[(1,)])
    runner = SqlRunner(_FakeConnection(cursor))

    runner.run_query("SELECT 1")

    assert cursor.closed is True


def test_run_query_closes_cursor_even_on_error():
    cursor = _FakeCursor(raise_on={"SELECT bad": RuntimeError("invalid identifier")})
    runner = SqlRunner(_FakeConnection(cursor))

    with pytest.raises(RuntimeError, match="invalid identifier"):
        runner.run_query("SELECT bad")

    assert cursor.closed is True


def test_run_query_applies_session_context_before_executing():
    cursor = _FakeCursor(description=[("X",)], rows=[(1,)])
    runner = SqlRunner(_FakeConnection(cursor))

    runner.run_query("SELECT 1", database="DB", schema="PUBLIC", warehouse="WH", role="SYSADMIN")

    assert cursor.executed == [
        "USE ROLE SYSADMIN",
        "USE WAREHOUSE WH",
        "USE DATABASE DB",
        "USE SCHEMA PUBLIC",
        "SELECT 1",
    ]


def test_run_query_skips_use_statements_when_context_not_given():
    """Matters for `snowrig serve`: the connection already has its session
    context set at connect() time, so run_query() with no context args
    shouldn't emit any USE statements at all."""
    cursor = _FakeCursor(description=[("X",)], rows=[(1,)])
    runner = SqlRunner(_FakeConnection(cursor))

    runner.run_query("SELECT 1")

    assert cursor.executed == ["SELECT 1"]


# --------------------------------------------------------------------- #
# run() — dict-per-row shape, used by `snowrig exec`
# --------------------------------------------------------------------- #

def test_run_returns_list_of_dicts():
    cursor = _FakeCursor(description=[("ID",), ("NAME",)], rows=[(1, "Alice"), (2, "Bob")])
    runner = SqlRunner(_FakeConnection(cursor))

    rows = runner.run("SELECT id, name FROM customers")

    assert rows == [{"ID": 1, "NAME": "Alice"}, {"ID": 2, "NAME": "Bob"}]


# --------------------------------------------------------------------- #
# run_transaction() — used by `snowrig transaction`
# --------------------------------------------------------------------- #

def test_run_transaction_commits_on_success():
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    runner = SqlRunner(conn)

    runner.run_transaction(["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"])

    assert conn.committed is True
    assert conn.rolled_back is False
    assert cursor.executed == ["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"]


def test_run_transaction_rolls_back_on_error_and_reraises():
    cursor = _FakeCursor(raise_on={"BAD SQL": RuntimeError("syntax error")})
    conn = _FakeConnection(cursor)
    runner = SqlRunner(conn)

    with pytest.raises(RuntimeError, match="syntax error"):
        runner.run_transaction(["INSERT INTO t VALUES (1)", "BAD SQL"])

    assert conn.rolled_back is True
    assert conn.committed is False


def test_run_transaction_restores_autocommit_even_on_error():
    cursor = _FakeCursor(raise_on={"BAD SQL": RuntimeError("boom")})
    conn = _FakeConnection(cursor)
    runner = SqlRunner(conn)

    with pytest.raises(RuntimeError):
        runner.run_transaction(["BAD SQL"])

    assert conn.autocommit_calls == [False, True]