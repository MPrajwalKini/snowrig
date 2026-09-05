"""Runs arbitrary SQL through the official Snowflake Connector for Python,
replacing snowrig's original hand-rolled SQL API v2 client (async HTTP,
manual statement polling, manual multi-statement counting). The connector
already handles auth, retries, and result formatting — and gives real
transactions via connection.commit()/rollback() instead of us having to
hand-build BEGIN/COMMIT SQL text.
"""

from __future__ import annotations

from typing import Any


class SqlRunner:
    def __init__(self, connection: Any):
        self._conn = connection

    def _use_context(
        self, cur: Any, *, database: str | None, schema: str | None,
        warehouse: str | None, role: str | None,
    ) -> None:
        if role:
            cur.execute(f"USE ROLE {role}")
        if warehouse:
            cur.execute(f"USE WAREHOUSE {warehouse}")
        if database:
            cur.execute(f"USE DATABASE {database}")
        if schema:
            cur.execute(f"USE SCHEMA {schema}")

    def run(
        self,
        sql: str,
        *,
        database: str | None = None,
        schema: str | None = None,
        warehouse: str | None = None,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            self._use_context(cur, database=database, schema=schema, warehouse=warehouse, role=role)
            cur.execute(sql)
            if cur.description:
                columns = [c[0] for c in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
            return []
        finally:
            cur.close()

    def run_transaction(
        self,
        statements: list[str],
        *,
        database: str | None = None,
        schema: str | None = None,
        warehouse: str | None = None,
        role: str | None = None,
    ) -> None:
        """Runs every statement atomically — all commit together, or none do."""
        cur = self._conn.cursor()
        try:
            self._use_context(cur, database=database, schema=schema, warehouse=warehouse, role=role)
            self._conn.autocommit(False)
            for stmt in statements:
                cur.execute(stmt)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.autocommit(True)
            cur.close()

    def call_procedure(
        self,
        qualified_name: str,
        args: list[Any],
        **context: Any,
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("%s" for _ in args) if args else ""
        return self._run_with_params(f"CALL {qualified_name}({placeholders})", args, **context)

    def _run_with_params(
        self, sql: str, params: list[Any],
        *, database: str | None = None, schema: str | None = None,
        warehouse: str | None = None, role: str | None = None,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            self._use_context(cur, database=database, schema=schema, warehouse=warehouse, role=role)
            cur.execute(sql, params)
            if cur.description:
                columns = [c[0] for c in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
            return []
        finally:
            cur.close()
