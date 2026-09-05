from __future__ import annotations

import pytest

from snowrig.manifest.diff import Action, apply_plan, compute_plan
from snowrig.manifest.schema import ManifestObject
from tests.fakes import FakeCoreObjectClient


def _table(name: str, columns: list[dict], **extra) -> ManifestObject:
    return ManifestObject(
        resource="table",
        path_params={"database": "DB", "schema": "PUBLIC", "name": name},
        body={"columns": columns, **extra},
    )


# --------------------------------------------------------------------- #
# compute_plan
# --------------------------------------------------------------------- #

def test_create_when_object_does_not_exist():
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    client = FakeCoreObjectClient(live={})

    [change] = compute_plan([obj], client)

    assert change.action == Action.CREATE
    assert change.diff == {}
    assert not change.is_destructive


def test_noop_when_live_matches_desired():
    columns = [{"name": "ID", "datatype": "NUMBER(38,0)"}]
    obj = _table("CUSTOMERS", columns)
    client = FakeCoreObjectClient(live={("table", "CUSTOMERS"): {"columns": columns}})

    [change] = compute_plan([obj], client)

    assert change.action == Action.NOOP
    assert change.diff == {}


def test_additive_column_is_update_but_not_destructive():
    """Manifest adds a column the live table doesn't have yet."""
    obj = _table(
        "CUSTOMERS",
        [
            {"name": "ID", "datatype": "NUMBER(38,0)"},
            {"name": "EMAIL", "datatype": "VARCHAR(255)"},
        ],
    )
    client = FakeCoreObjectClient(
        live={("table", "CUSTOMERS"): {"columns": [{"name": "ID", "datatype": "NUMBER(38,0)"}]}}
    )

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert not change.is_destructive
    assert change.diff["columns"].removed_items == []


def test_column_removed_from_manifest_is_flagged_destructive():
    """The scenario from the review: live has EMAIL, manifest doesn't."""
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    client = FakeCoreObjectClient(
        live={
            ("table", "CUSTOMERS"): {
                "columns": [
                    {"name": "ID", "datatype": "NUMBER(38,0)"},
                    {"name": "EMAIL", "datatype": "VARCHAR(255)"},
                ]
            }
        }
    )

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert change.is_destructive
    assert change.diff["columns"].removed_items == ["EMAIL"]
    assert "EMAIL" in change.destructive_summary()


def test_scalar_field_change_is_update_not_destructive():
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}], comment="new comment")
    client = FakeCoreObjectClient(
        live={
            ("table", "CUSTOMERS"): {
                "columns": [{"name": "ID", "datatype": "NUMBER(38,0)"}],
                "comment": "old comment",
            }
        }
    )

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert not change.is_destructive
    assert change.diff["comment"].live == "old comment"
    assert change.diff["comment"].desired == "new comment"


def test_sql_backed_object_always_update_and_never_destructive():
    obj = ManifestObject(
        resource="procedure",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_PROC"},
        body={"arguments": []},
        sql="create or replace procedure my_proc() returns varchar language sql as $$ select 1 $$;",
    )
    client = FakeCoreObjectClient(live={})

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert not change.is_destructive


def test_error_action_on_unexpected_fetch_failure():
    class BoomClient(FakeCoreObjectClient):
        def fetch(self, resource, path_params):
            raise RuntimeError("(400) Reason: Bad Request")

    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    [change] = compute_plan([obj], BoomClient())

    assert change.action == Action.ERROR
    assert change.error is not None


# --------------------------------------------------------------------- #
# apply_plan
# --------------------------------------------------------------------- #

def _plan_for_destructive_change():
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    client = FakeCoreObjectClient(
        live={
            ("table", "CUSTOMERS"): {
                "columns": [
                    {"name": "ID", "datatype": "NUMBER(38,0)"},
                    {"name": "EMAIL", "datatype": "VARCHAR(255)"},
                ]
            }
        }
    )
    return compute_plan([obj], client), client


def test_destructive_change_is_blocked_by_default():
    plan, client = _plan_for_destructive_change()

    results = apply_plan(plan, client)

    [(change, error)] = results
    assert change.blocked is not None
    assert "allow-destructive" in change.blocked
    assert error == change.blocked
    assert client.applied == []
    assert client.live[("table", "CUSTOMERS")]["columns"] == [
        {"name": "ID", "datatype": "NUMBER(38,0)"},
        {"name": "EMAIL", "datatype": "VARCHAR(255)"},
    ]


def test_destructive_change_proceeds_with_allow_destructive():
    plan, client = _plan_for_destructive_change()

    results = apply_plan(plan, client, allow_destructive=True)

    [(change, error)] = results
    assert error is None
    assert change.blocked is None
    assert len(client.applied) == 1
    resource, path_params, body = client.applied[0]
    assert resource == "table"
    assert body["columns"] == [{"name": "ID", "datatype": "NUMBER(38,0)"}]


def test_noop_changes_are_not_applied():
    columns = [{"name": "ID", "datatype": "NUMBER(38,0)"}]
    obj = _table("CUSTOMERS", columns)
    client = FakeCoreObjectClient(live={("table", "CUSTOMERS"): {"columns": columns}})
    plan = compute_plan([obj], client)

    results = apply_plan(plan, client)

    assert results == []
    assert client.applied == []


def test_stop_on_error_halts_remaining_changes():
    good = _table("A", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    bad = _table("B", [{"name": "ID", "datatype": "NUMBER(38,0)"}])

    class FailingClient(FakeCoreObjectClient):
        def create_or_alter(self, resource, path_params, body):
            if path_params["name"] == "A":
                raise RuntimeError("boom")
            super().create_or_alter(resource, path_params, body)

    client = FailingClient(live={})
    plan = compute_plan([good, bad], client)

    results = apply_plan(plan, client, stop_on_error=True)

    assert len(results) == 1
    assert results[0][1] is not None
    assert client.applied == []


def test_continue_on_error_applies_remaining_changes():
    good = _table("A", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    bad = _table("B", [{"name": "ID", "datatype": "NUMBER(38,0)"}])

    class FailingClient(FakeCoreObjectClient):
        def create_or_alter(self, resource, path_params, body):
            if path_params["name"] == "A":
                raise RuntimeError("boom")
            super().create_or_alter(resource, path_params, body)

    client = FailingClient(live={})
    plan = compute_plan([good, bad], client)

    results = apply_plan(plan, client, stop_on_error=False)

    assert len(results) == 2
    assert results[0][1] is not None
    assert results[1][1] is None
    assert [a[1]["name"] for a in client.applied] == ["B"]


def test_dry_run_reports_without_applying():
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER(38,0)"}])
    client = FakeCoreObjectClient(live={})
    plan = compute_plan([obj], client)

    results = apply_plan(plan, client, dry_run=True)

    [(change, error)] = results
    assert error is None
    assert client.applied == []