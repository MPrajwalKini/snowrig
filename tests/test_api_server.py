"""Tests for snowrig.api.server — the `snowrig serve` HTTP API that lets
other platforms run SQL through a named profile without ever holding
Snowflake credentials themselves.

connect() is monkeypatched to a fake connection/cursor so these tests
never touch a real Snowflake account. What's actually being verified:
auth is enforced on every non-health endpoint, /v1/profiles never leaks
key material or passphrases, and the query/test-connection paths handle
both success and failure correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import snowrig.api.server as server_module

TOKEN_ENV = "SNOWRIG_API_TOKEN_TEST"
VALID_TOKEN = "test-token-abc123"


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
profiles:
  default:
    account: myorg-acct
    user: SVC_USER
    private_key: |
      -----BEGIN PRIVATE KEY-----
      fake-key-content-not-real
      -----END PRIVATE KEY-----
    warehouse: COMPUTE_WH
    role: SYSADMIN
    database: MY_DB
    schema: PUBLIC
  readonly:
    account: myorg-acct
    user: RO_USER
    private_key: |
      -----BEGIN PRIVATE KEY-----
      another-fake-key
      -----END PRIVATE KEY-----
"""
    )
    return config_path


class _FakeCursor:
    def __init__(self, *, description=None, rows=None, rowcount=0, raise_on_execute=None):
        self.description = description
        self._rows = rows or []
        self.rowcount = rowcount
        self._raise_on_execute = raise_on_execute
        self.closed = False
        self.executed_with_params: list[tuple] = []

    def execute(self, sql, *args):
        self.executed_with_params.append((sql, args))
        if self._raise_on_execute:
            raise self._raise_on_execute
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self._closed = False

    def cursor(self):
        return self._cursor

    def is_closed(self):
        return self._closed


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, VALID_TOKEN)
    config_path = _write_config(tmp_path)
    app = server_module.build_app(config_path=config_path, token_env=TOKEN_ENV)
    return TestClient(app)


def _auth_headers(token: str = VALID_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- #
# health — no auth required
# --------------------------------------------------------------------- #

def test_health_requires_no_auth(client):
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --------------------------------------------------------------------- #
# auth gating
# --------------------------------------------------------------------- #

def test_profiles_rejects_missing_auth_header(client):
    response = client.get("/v1/profiles")

    assert response.status_code == 401


def test_profiles_rejects_wrong_token(client):
    response = client.get("/v1/profiles", headers=_auth_headers("wrong-token"))

    assert response.status_code == 401


def test_query_rejects_missing_auth(client):
    response = client.post("/v1/query", json={"profile": "default", "sql": "SELECT 1"})

    assert response.status_code == 401


def test_server_misconfigured_when_token_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config_path = _write_config(tmp_path)
    app = server_module.build_app(config_path=config_path, token_env=TOKEN_ENV)
    client = TestClient(app)

    response = client.get("/v1/profiles", headers=_auth_headers())

    assert response.status_code == 500
    assert TOKEN_ENV in response.json()["detail"]


# --------------------------------------------------------------------- #
# /v1/profiles — must never leak key material
# --------------------------------------------------------------------- #

def test_profiles_lists_names_without_key_material(client):
    response = client.get("/v1/profiles", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"default", "readonly"}
    assert body["default"]["account"] == "myorg-acct"
    assert body["default"]["warehouse"] == "COMPUTE_WH"

    # The actual thing this endpoint must never do:
    dumped = str(body)
    assert "fake-key-content-not-real" not in dumped
    assert "another-fake-key" not in dumped
    assert "private_key" not in dumped
    assert "passphrase" not in dumped.lower()


# --------------------------------------------------------------------- #
# /v1/query
# --------------------------------------------------------------------- #

def test_query_unknown_profile_returns_404(client):
    response = client.post(
        "/v1/query", json={"profile": "nonexistent", "sql": "SELECT 1"}, headers=_auth_headers()
    )

    assert response.status_code == 404


def test_query_success_returns_columns_and_rows(client, monkeypatch):
    cursor = _FakeCursor(description=[("ID",), ("NAME",)], rows=[(1, "Alice"), (2, "Bob")], rowcount=2)
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/query",
        json={"profile": "default", "sql": "SELECT id, name FROM customers"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["columns"] == ["ID", "NAME"]
    assert body["rows"] == [[1, "Alice"], [2, "Bob"]]
    assert body["rowcount"] == 2
    assert cursor.closed is True  # cursor always closed, even on success

def test_query_with_params_binds_them_natively(client, monkeypatch):
    """A caller sending {"sql": "... WHERE id = %s", "params": [123]} must
    have that value bound by the driver, not string-formatted into the SQL
    — this is the whole point of /v1/query accepting params separately."""
    cursor = _FakeCursor(description=[("NAME",)], rows=[("Alice",)], rowcount=1)
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/query",
        json={"profile": "default", "sql": "SELECT name FROM customers WHERE id = %s", "params": [123]},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert cursor.executed_with_params == [
        ("SELECT name FROM customers WHERE id = %s", ([123],))
    ]


def test_query_without_params_field_still_works(client, monkeypatch):
    """params is optional — omitting it entirely from the request body
    (not even sending null) must behave exactly like today."""
    cursor = _FakeCursor(description=[("X",)], rows=[(1,)], rowcount=1)
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/query", json={"profile": "default", "sql": "SELECT 1"}, headers=_auth_headers()
    )

    assert response.status_code == 200
    assert cursor.executed_with_params == [("SELECT 1", ())]
    
def test_query_sql_error_returns_400_not_500(client, monkeypatch):
    cursor = _FakeCursor(raise_on_execute=RuntimeError("SQL compilation error: invalid identifier"))
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/query", json={"profile": "default", "sql": "SELECT nope"}, headers=_auth_headers()
    )

    assert response.status_code == 400
    assert "invalid identifier" in response.json()["detail"]
    assert cursor.closed is True  # finally: block still closes the cursor on error


def test_query_reuses_cached_connection_across_requests(client, monkeypatch):
    connect_calls = []

    def _fake_connect(profile):
        connect_calls.append(profile)
        return _FakeConnection(_FakeCursor(description=[("X",)], rows=[(1,)], rowcount=1))

    monkeypatch.setattr(server_module, "connect", _fake_connect)

    client.post("/v1/query", json={"profile": "default", "sql": "SELECT 1"}, headers=_auth_headers())
    client.post("/v1/query", json={"profile": "default", "sql": "SELECT 2"}, headers=_auth_headers())

    assert len(connect_calls) == 1  # second request reused the cached connection


def test_query_reconnects_if_cached_connection_is_closed(client, monkeypatch):
    connect_calls = []

    def _fake_connect(profile):
        conn = _FakeConnection(_FakeCursor(description=[("X",)], rows=[(1,)], rowcount=1))
        connect_calls.append(conn)
        return conn

    monkeypatch.setattr(server_module, "connect", _fake_connect)

    client.post("/v1/query", json={"profile": "default", "sql": "SELECT 1"}, headers=_auth_headers())
    connect_calls[0]._closed = True  # simulate the connection dying between requests
    client.post("/v1/query", json={"profile": "default", "sql": "SELECT 2"}, headers=_auth_headers())

    assert len(connect_calls) == 2


# --------------------------------------------------------------------- #
# /v1/test-connection
# --------------------------------------------------------------------- #

def test_test_connection_success(client, monkeypatch):
    cursor = _FakeCursor(rows=[(1,)])
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/test-connection", json={"profile": "default"}, headers=_auth_headers()
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_test_connection_failure_reports_error_not_500(client, monkeypatch):
    cursor = _FakeCursor(raise_on_execute=RuntimeError("Connection refused"))
    monkeypatch.setattr(server_module, "connect", lambda profile: _FakeConnection(cursor))

    response = client.post(
        "/v1/test-connection", json={"profile": "default"}, headers=_auth_headers()
    )

    assert response.status_code == 200  # reports failure in the body, not an HTTP error
    body = response.json()
    assert body["ok"] is False
    assert "Connection refused" in body["error"]


def test_test_connection_unknown_profile_returns_404(client):
    response = client.post(
        "/v1/test-connection", json={"profile": "nonexistent"}, headers=_auth_headers()
    )

    assert response.status_code == 404