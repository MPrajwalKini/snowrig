"""Tests for snowrig.Session / snowrig.session().

test_public_api.py already covers connection lifecycle for the one-shot
plan()/apply() functions (which now delegate to Session internally — see
those tests for why that refactor didn't change their observable
behavior). What's specific to Session and needs its own coverage: a
single connection gets reused across multiple plan()/apply()/query()
calls within one `with` block, and using a Session outside a `with`
block fails clearly instead of silently doing nothing.
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
    fake_client = FakeCoreObjectClient(live={})

    monkeypatch.setattr(snowrig, "CoreObjectClient", lambda root: fake_client)
    monkeypatch.setattr(snowrig, "Root", lambda conn: conn)
    monkeypatch.setattr(snowrig, "SqlRunner", lambda conn: _FakeSqlRunner())

    class _FakeProfile:
        warehouse = "COMPUTE_WH"
        role = "SYSADMIN"

    opened_connections = []

    def _fake_open_connection(profile):
        conn = _FakeConnection()
        opened_connections.append(conn)
        return conn, _FakeProfile.warehouse, _FakeProfile.role

    monkeypatch.setattr(snowrig, "_open_connection", _fake_open_connection)

    return opened_connections


class _FakeSqlRunner:
    def run_query(self, sql, params=None):
        return (["X"], [(1,)], 1)


def test_session_opens_connection_only_on_enter_not_on_construction(tmp_path, _patch_snowrig_internals):
    s = snowrig.session(profile="default")

    assert _patch_snowrig_internals == []  # constructing session() doesn't open anything

    with s:
        assert len(_patch_snowrig_internals) == 1


def test_session_reuses_one_connection_across_multiple_calls(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    with snowrig.session(profile="default") as s:
        s.plan(manifest_dir)
        s.plan(manifest_dir)
        s.apply(manifest_dir)

    assert len(_patch_snowrig_internals) == 1  # exactly one connection opened, not three


def test_session_closes_owned_connection_on_exit(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    with snowrig.session(profile="default") as s:
        s.plan(manifest_dir)

    [conn] = _patch_snowrig_internals
    assert conn.closed is True


def test_session_does_not_close_caller_supplied_connection_on_exit(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)
    caller_conn = _FakeConnection()

    with snowrig.session(connection=caller_conn) as s:
        s.plan(manifest_dir)

    assert caller_conn.closed is False
    assert _patch_snowrig_internals == []  # never opened one of its own


def test_session_closes_connection_even_if_a_call_raises(tmp_path, monkeypatch, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(snowrig, "compute_plan", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        with snowrig.session(profile="default") as s:
            s.plan(manifest_dir)

    [conn] = _patch_snowrig_internals
    assert conn.closed is True


def test_calling_plan_outside_a_with_block_raises_clearly():
    s = snowrig.Session(profile="default")

    with pytest.raises(RuntimeError, match="not open"):
        s.plan("some/manifest/dir")


def test_calling_apply_outside_a_with_block_raises_clearly():
    s = snowrig.Session(profile="default")

    with pytest.raises(RuntimeError, match="not open"):
        s.apply("some/manifest/dir")


def test_calling_query_outside_a_with_block_raises_clearly():
    s = snowrig.Session(profile="default")

    with pytest.raises(RuntimeError, match="not open"):
        s.query("SELECT 1")


def test_session_query_delegates_to_sql_runner(tmp_path, _patch_snowrig_internals):
    with snowrig.session(profile="default") as s:
        columns, rows, rowcount = s.query("SELECT 1")

    assert columns == ["X"]
    assert rows == [(1,)]
    assert rowcount == 1


def test_session_apply_forwards_profile_warehouse_and_role(tmp_path, monkeypatch, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)
    captured_kwargs = {}

    def _spy_apply_plan(plan_result, client, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(snowrig, "apply_plan", _spy_apply_plan)

    with snowrig.session(profile="default") as s:
        s.apply(manifest_dir)

    assert captured_kwargs["warehouse"] == "COMPUTE_WH"
    assert captured_kwargs["role"] == "SYSADMIN"


def test_session_after_exit_cannot_be_reused(tmp_path, _patch_snowrig_internals):
    manifest_dir = _write_manifest(tmp_path)
    s = snowrig.session(profile="default")

    with s:
        s.plan(manifest_dir)

    with pytest.raises(RuntimeError, match="not open"):
        s.plan(manifest_dir)