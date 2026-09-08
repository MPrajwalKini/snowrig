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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from snowrig.config import Profile, load_profiles_from_file
from snowrig.connection import connect


def _unauthenticated_error(detail: str):
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail=detail)


def build_app(config_path: Path | None = None, token_env: str = "SNOWRIG_API_TOKEN"):
    """Builds the FastAPI app. Raises ImportError with a clear message if
    the `api` extra isn't installed."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "snowrig serve needs the 'api' extra: pip install snowrig[api]"
        ) from e

    app = FastAPI(title="snowrig connection API", version="1.0")
    profiles: dict[str, Profile] = load_profiles_from_file(config_path)
    connections: dict[str, Any] = {}  # lazy per-profile connection cache

    class QueryRequest(BaseModel):
        profile: str
        sql: str

    class TestRequest(BaseModel):
        profile: str

    def _authed(request: Request) -> None:
        expected = os.environ.get(token_env)
        if not expected:
            raise HTTPException(
                status_code=500,
                detail=f"server misconfigured: {token_env} is not set in its environment",
            )
        got = request.headers.get("authorization", "")
        if got != f"Bearer {expected}":
            raise _unauthenticated_error("missing or invalid bearer token")

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
            conn.cursor().execute("SELECT 1").fetchone()
            return {"ok": True}
        except Exception as e:
            connections.pop(body.profile, None)
            return {"ok": False, "error": str(e)}

    @app.post("/v1/query")
    def query(body: QueryRequest, request: Request) -> dict:
        _authed(request)
        conn = _get_connection(body.profile)
        cur = conn.cursor()
        try:
            cur.execute(body.sql)
            columns = [c[0] for c in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
            return {"columns": columns, "rows": rows, "rowcount": cur.rowcount}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        finally:
            cur.close()

    return app