"""Tests for the public snowrig.plan()/snowrig.apply() API.

The behavior worth locking down here isn't the diffing logic itself
(that's covered by test_diff.py/test_graph.py) — it's the connection
lifecycle: snowrig must open+close its own connection when given a
profile, and must NEVER close a connection the caller handed it via
`connection=`, since that's the whole point of the library positioning
(embedding inside code that manages its own connection pool).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import snowrig
from tests.fakes import FakeCoreObjectClient


def _write_manifest(tmp_path: Path) -> str:
    (tmp_path / "warehouse.yaml").write_text(
        "resource: warehouse\npath_params:\n  name: COMPUTE_WH\nbody: {}\n"
    )
    return str(tmp_path)


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def cursor(self):
        raise NotImplementedError  # not needed for these tests


@pytest.fixture(autouse=True)
def _patch_snowrig_internals(monkeypatch):
    """Replaces the pieces snowrig.plan()/apply() call out to, so these
    tests exercise only connection lifecycle and delegation, not real
    Snowflake I/O or the diff engine (already covered elsewhere)."""

    fake_client = FakeCoreObjectClient(live={})

    monkeypatch.setattr(snowrig, "CoreObjectClient", lambda root: fake_client)
    monkeypatch.setattr(snowrig, "Root", lambda conn: conn)  # pass-through; unused by the fake client
    monkeypatch.setattr(snowrig, "SqlRunner", lambda conn: object())

    class _FakeProfile:
        warehouse = "COMPUTE_WH"
        role = "SYSADMIN"

    opened_connections = []

    def _fake_open_connection(profile):
        conn = _FakeConnection()
        opened_connections.append(conn)
        return conn, _FakeProfile.warehouse, _FakeProfile.role

    monkeypatch.setattr(snowrig, "_open_connection", _fake_open_connection)

    return opened_connections  # exposed via the fixture's return value


def test_plan_opens_and_closes_its_own_connection(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    result = snowrig.plan(manifest_dir, profile="default")

    [conn] = _patch_snowrig_internals
    assert conn.closed is True
    assert isinstance(result, list)


def test_plan_does_not_close_a_caller_supplied_connection(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)
    caller_conn = _FakeConnection()

    snowrig.plan(manifest_dir, connection=caller_conn)

    assert caller_conn.closed is False
    assert _patch_snowrig_internals == []  # snowrig never opened one of its own


def test_apply_opens_and_closes_its_own_connection(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    snowrig.apply(manifest_dir, profile="default")

    [conn] = _patch_snowrig_internals
    assert conn.closed is True


def test_apply_does_not_close_a_caller_supplied_connection(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)
    caller_conn = _FakeConnection()

    snowrig.apply(manifest_dir, connection=caller_conn)

    assert caller_conn.closed is False
    assert _patch_snowrig_internals == []


def test_connection_is_closed_even_if_compute_plan_raises(tmp_path, monkeypatch, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(snowrig, "compute_plan", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        snowrig.plan(manifest_dir, profile="default")

    [conn] = _patch_snowrig_internals
    assert conn.closed is True  # finally: block must still run


def test_plan_returns_planned_changes_via_fake_client(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    result = snowrig.plan(manifest_dir, profile="default")

    assert len(result) == 1
    assert result[0].action == snowrig.Action.CREATE  # warehouse not in the fake client's live state

def test_apply_forwards_profile_warehouse_and_role_to_apply_plan(
    tmp_path, monkeypatch, _patch_snowrig_internals
):
    """Guards the thing that was previously unverified: apply() must
    actually thread the profile's warehouse/role through to apply_plan(),
    not just resolve them and drop them."""
    manifest_dir = _write_manifest(tmp_path)
    captured_kwargs = {}

    def _spy_apply_plan(plan_result, client, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(snowrig, "apply_plan", _spy_apply_plan)

    snowrig.apply(manifest_dir, profile="default")

    assert captured_kwargs["warehouse"] == "COMPUTE_WH"
    assert captured_kwargs["role"] == "SYSADMIN"


def test_apply_passes_none_warehouse_and_role_for_caller_supplied_connection(
    tmp_path, monkeypatch, _patch_snowrig_internals
):
    """With a caller-supplied connection, snowrig never consulted a
    profile, so it must not invent a warehouse/role — the connection's own
    session context (set up by the caller) should be what's used, meaning
    apply_plan gets None/None and its `if warehouse:` / `if role:` no-ops
    just fall through to SqlRunner's USE-statement skip."""
    manifest_dir = _write_manifest(tmp_path)
    captured_kwargs = {}

    def _spy_apply_plan(plan_result, client, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(snowrig, "apply_plan", _spy_apply_plan)

    snowrig.apply(manifest_dir, connection=_FakeConnection())

    assert captured_kwargs["warehouse"] is None
    assert captured_kwargs["role"] is None