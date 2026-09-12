"""FastAPI app: run Snowflake SQL from any platform that speaks HTTP.

pip install snowrig[api]
export SNOWRIG_API_TOKEN=$(openssl rand -hex 32)
snowrig serve --config ~/.snowrig/config.yaml

Every request needs `Authorization: Bearer <SNOWRIG_API_TOKEN>`. The
private key for every profile stays on the machine running `snowrig
serve` — callers only ever send a profile name and SQL, and get rows
back. That's the point: platforms that don't have (or shouldn't have) a
Snowflake driver, key material, or a client library can still run
queries.

Not a general-purpose API gateway: no query allowlisting, no per-caller
row-level auth, no rate limiting. Put this behind your own gateway/proxy
if you need those, rather than exposing --host 0.0.0.0 directly.

Deliberately NOT using `from __future__ import annotations` here: FastAPI
resolves parameter type hints by looking them up in the function's
*global* namespace at request-handling time. With postponed evaluation,
every annotation becomes a string, and any type defined inside a
function (a closure-local, not a global) can't be resolved back from
that string — FastAPI silently falls back to treating the parameter as a
plain query parameter instead of an injected Request or a parsed request
body. That's why QueryRequest/TestRequest/Request are all defined here
at module scope, with real (non-deferred) annotations.
"""

import hmac
import os
from pathlib import Path
from typing import Any, Optional

from snowrig.config import Profile, load_profiles_from_file
from snowrig.connection import connect
from snowrig.sql import SqlRunner

try:
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "snowrig serve needs the 'api' extra: pip install snowrig[api]"
    ) from e


class QueryRequest(BaseModel):
    profile: str
    sql: str
    params: Optional[list] = None


class TestRequest(BaseModel):
    profile: str


def build_app(config_path: Optional[Path] = None, token_env: str = "SNOWRIG_API_TOKEN") -> FastAPI:
    app = FastAPI(title="snowrig connection API", version="1.0")
    profiles: dict = load_profiles_from_file(config_path)
    connections: dict = {}  # lazy per-profile connection cache

    def _authed(request: Request) -> None:
        expected = os.environ.get(token_env)
        if not expected:
            raise HTTPException(
                status_code=500,
                detail=f"server misconfigured: {token_env} is not set in its environment",
            )
        got = request.headers.get("authorization", "")
        # Constant-time compare — a plain `!=` short-circuits on the first
        # mismatched byte, which leaks how many leading characters of the
        # token a guess got right via response-time differences.
        if not hmac.compare_digest(got, f"Bearer {expected}"):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    def _get_connection(profile_name: str):
        if profile_name not in profiles:
            raise HTTPException(status_code=404, detail=f"unknown profile '{profile_name}'")
        conn = connections.get(profile_name)
        if conn is None or conn.is_closed():
            conn = connect(profiles[profile_name])
            connections[profile_name] = conn
        return conn

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/v1/profiles")
    def list_profiles(request: Request) -> dict:
        _authed(request)
        # Never returns key material or passphrases — only what's needed
        # for a caller to pick the right profile name.
        return {
            name: {
                "account": p.account,
                "user": p.user,
                "warehouse": p.warehouse,
                "role": p.role,
                "database": p.database,
                "schema": p.schema,
            }
            for name, p in profiles.items()
        }

    @app.post("/v1/test-connection")
    def test_connection(body: TestRequest, request: Request) -> dict:
        _authed(request)
        if body.profile not in profiles:
            raise HTTPException(status_code=404, detail=f"unknown profile '{body.profile}'")
        try:
            conn = _get_connection(body.profile)
            SqlRunner(conn).run_query("SELECT 1")
            return {"ok": True}
        except Exception as e:
            connections.pop(body.profile, None)
            return {"ok": False, "error": str(e)}

    @app.post("/v1/query")
    def query(body: QueryRequest, request: Request) -> dict:
        _authed(request)
        conn = _get_connection(body.profile)
        try:
            columns, rows, rowcount = SqlRunner(conn).run_query(body.sql, body.params)
            return {"columns": columns, "rows": rows, "rowcount": rowcount}
        except Exception as e:
            # Mirrors test-connection's cache eviction: if the failure left
            # the connection itself closed (a dropped network connection, an
            # expired session, ...), drop it from the cache so the *next*
            # request reconnects instead of repeatedly retrying a connection
            # that's already dead. A plain SQL error (bad syntax, missing
            # table) leaves the connection open, so it's kept and reused —
            # only actual connection failures trigger eviction.
            if conn.is_closed():
                connections.pop(body.profile, None)
            raise HTTPException(status_code=400, detail=str(e)) from e

    return app