"""Client for the Snowflake SQL API v2 (/api/v2/statements).

This is the execution surface: ad-hoc queries, DML, calling stored
procedures, and (via the object registry's create-or-alter payloads)
a fallback path for DDL that the Object Management API doesn't yet
cover. Statement execution is asynchronous server-side; we poll until
the statement resolves.

Reference: https://docs.snowflake.com/en/developer-guide/sql-api
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from snowrig.client.http import SnowflakeHttpClient, SnowflakeHttpError

_POLL_INTERVAL_SECONDS = 0.75
_DEFAULT_POLL_TIMEOUT_SECONDS = 300


@dataclass
class StatementResult:
    statement_handle: str
    status_code: int
    resultSetMetaData: dict | None = None
    data: list[list[Any]] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    statement_handles: list[str] = field(default_factory=list)

    @property
    def columns(self) -> list[str]:
        if not self.resultSetMetaData:
            return []
        return [c["name"] for c in self.resultSetMetaData.get("rowType", [])]

    def as_dicts(self) -> list[dict[str, Any]]:
        cols = self.columns
        return [dict(zip(cols, row)) for row in self.data]


class SqlApiClient:
    def __init__(self, http: SnowflakeHttpClient):
        self._http = http

    async def submit(
        self,
        statement: str,
        *,
        database: str | None = None,
        schema: str | None = None,
        warehouse: str | None = None,
        role: str | None = None,
        bindings: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        async_exec: bool = False,
        request_id: str | None = None,
    ) -> StatementResult:
        """Submit a single SQL statement and wait for it to finish (unless async_exec)."""
        body: dict[str, Any] = {"statement": statement}
        if database:
            body["database"] = database
        if schema:
            body["schema"] = schema
        if warehouse:
            body["warehouse"] = warehouse
        if role:
            body["role"] = role
        if bindings:
            body["bindings"] = bindings
        if timeout_seconds:
            body["timeout"] = timeout_seconds

        params = {"requestId": request_id or str(uuid.uuid4())}
        headers = {"Accept": "application/json"} if async_exec else {}

        resp = await self._http.request(
            "POST", "/api/v2/statements", json_body=body, params=params,
            extra_headers=headers,
        )
        payload = resp.json()

        if resp.status_code == 202 and not async_exec:
            handle = payload["statementHandle"]
            return await self.wait(handle)

        return _to_result(payload, resp.status_code)

    async def get_status(self, statement_handle: str) -> StatementResult:
        resp = await self._http.request(
            "GET", f"/api/v2/statements/{statement_handle}"
        )
        return _to_result(resp.json(), resp.status_code)

    async def wait(
        self, statement_handle: str, timeout_seconds: int = _DEFAULT_POLL_TIMEOUT_SECONDS
    ) -> StatementResult:
        elapsed = 0.0
        while elapsed < timeout_seconds:
            result = await self.get_status(statement_handle)
            # Snowflake returns 202 (still running) vs 200 (done) at the HTTP
            # layer; get_status surfaces status_code from the raw response.
            if result.status_code != 202:
                return result
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS
        raise TimeoutError(
            f"Statement {statement_handle} did not complete within {timeout_seconds}s"
        )

    async def cancel(self, statement_handle: str) -> None:
        await self._http.request(
            "POST", f"/api/v2/statements/{statement_handle}/cancel"
        )

    async def submit_multi(
        self,
        statements: str,
        **kwargs: Any,
    ) -> StatementResult:
        """Submit a semicolon-separated multi-statement block.

        Snowflake requires an explicit statement count header for this. The
        count is computed with `split_top_level_statements`, which correctly
        ignores semicolons inside string literals and $$-quoted bodies (e.g.
        a stored procedure's JavaScript) — a naive `.split(';')` would
        overcount the moment any statement contains one of those.
        """
        count = len(split_top_level_statements(statements))
        return await self.submit(
            statements,
            **kwargs,
        ) if count <= 1 else await self._submit_with_count(statements, count, **kwargs)

    async def submit_transaction(
        self,
        statements: list[str],
        **kwargs: Any,
    ) -> StatementResult:
        """Wraps `statements` in BEGIN TRANSACTION / COMMIT and submits them as
        one multi-statement request, per Snowflake's documented pattern for
        explicit transactions via the SQL API. Returns a StatementResult whose
        `statement_handles` lists the handle for each statement in order,
        including the BEGIN and COMMIT — use those with `get_status()` to
        check any individual statement if the transaction as a whole fails.
        """
        body = "; ".join(s.rstrip(";") for s in statements)
        full = f"begin transaction; {body}; commit"
        count = len(statements) + 2
        return await self._submit_with_count(full, count, **kwargs)

    async def _submit_with_count(
        self, statements: str, count: int, **kwargs: Any
    ) -> StatementResult:
        body: dict[str, Any] = {
            "statement": statements,
            "parameters": {"MULTI_STATEMENT_COUNT": count},
        }
        for k in ("database", "schema", "warehouse", "role", "bindings", "timeout_seconds"):
            v = kwargs.get(k)
            if v:
                body["parameters" if k == "bindings" else k] = v
        resp = await self._http.request(
            "POST", "/api/v2/statements", json_body=body,
            params={"requestId": str(uuid.uuid4())},
        )
        payload = resp.json()
        if resp.status_code == 202:
            return await self.wait(payload["statementHandle"])
        return _to_result(payload, resp.status_code)


def _to_result(payload: dict, http_status_code: int) -> StatementResult:
    return StatementResult(
        statement_handle=payload.get("statementHandle", ""),
        status_code=http_status_code,
        resultSetMetaData=payload.get("resultSetMetaData"),
        data=payload.get("data", []),
        raw=payload,
        statement_handles=payload.get("statementHandles", []),
    )


def split_top_level_statements(sql: str) -> list[str]:
    """Splits a semicolon-joined SQL block into individual statements, correctly
    ignoring semicolons that appear inside single-quoted strings or $$-delimited
    (or $tag$-delimited) bodies — e.g. the JavaScript inside a stored procedure.

    This is what makes MULTI_STATEMENT_COUNT accurate: naively splitting on
    every ';' overcounts as soon as a procedure/function body contains one.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single_quote = False
    dollar_tag: str | None = None  # e.g. "$$" or "$tag$" once we're inside one

    while i < n:
        ch = sql[i]

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single_quote:
            if ch == "'" and sql[i : i + 2] != "''":
                in_single_quote = False
            buf.append(ch)
            i += 1
            continue

        if ch == "'":
            in_single_quote = True
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            # Look for a dollar-quote tag: $$ or $identifier$
            match_end = i + 1
            while match_end < n and (sql[match_end].isalnum() or sql[match_end] == "_"):
                match_end += 1
            if match_end < n and sql[match_end] == "$":
                dollar_tag = sql[i : match_end + 1]
                buf.append(dollar_tag)
                i = match_end + 1
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)

    return statements
