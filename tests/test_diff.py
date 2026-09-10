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

# --------------------------------------------------------------------- #
# Regression tests: bugs found via live smoke testing (see CHANGELOG)
# --------------------------------------------------------------------- #

def test_nested_dict_field_diffs_as_declared_subset():
    """A single nested-object field (e.g. Stream.stream_source) should only
    diff on the keys the manifest declared, not server-computed extras
    fetch() returns alongside them. Caught live: a stream showed as
    perpetually changed because live stream_source carried database_name/
    schema_name/append_only/src_type that the manifest never declared."""
    obj = ManifestObject(
        resource="stream",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_STREAM"},
        body={"stream_source": {"name": "CUSTOMERS"}},
    )
    client = FakeCoreObjectClient(live={
        ("stream", "MY_STREAM"): {
            "stream_source": {
                "name": "CUSTOMERS",
                "database_name": "DB",
                "schema_name": "PUBLIC",
                "append_only": False,
                "src_type": "table",
            },
        },
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.NOOP, change.diff


def test_nested_dict_field_still_detects_real_change():
    """The subset comparison shouldn't become a rubber stamp — a genuine
    change to a declared key inside a nested dict must still be caught."""
    obj = ManifestObject(
        resource="stream",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_STREAM"},
        body={"stream_source": {"name": "CUSTOMERS"}},
    )
    client = FakeCoreObjectClient(live={
        ("stream", "MY_STREAM"): {"stream_source": {"name": "ORDERS", "database_name": "DB"}},
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert "stream_source" in change.diff


def test_column_datatype_normalizes_number_shorthand():
    """Snowflake's fetch() always returns fully-qualified NUMBER(38,0),
    VARCHAR(16777216), etc. A manifest using the short form (NUMBER,
    VARCHAR) must not diff as changed forever. Caught live: plan()
    reported a phantom UPDATE on every single run for any table using
    shorthand types."""
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "NUMBER"}])
    client = FakeCoreObjectClient(live={
        ("table", "CUSTOMERS"): {"columns": [{"name": "ID", "datatype": "NUMBER(38,0)"}]},
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.NOOP, change.diff


def test_column_datatype_still_detects_real_type_change():
    obj = _table("CUSTOMERS", [{"name": "ID", "datatype": "VARCHAR(50)"}])
    client = FakeCoreObjectClient(live={
        ("table", "CUSTOMERS"): {"columns": [{"name": "ID", "datatype": "NUMBER(38,0)"}]},
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert "columns" in change.diff


def test_view_query_ignores_ddl_wrapper():
    """fetch() returns a view's `query` as the full CREATE VIEW ... AS
    <select> DDL text, not just the SELECT. Comparing that raw against the
    manifest's bare query would make every view diff as changed forever —
    caught live against a real account."""
    obj = ManifestObject(
        resource="view",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_VIEW"},
        body={"query": "SELECT ID FROM DB.PUBLIC.CUSTOMERS"},
    )
    client = FakeCoreObjectClient(live={
        ("view", "MY_VIEW"): {
            "query": "CREATE  OR REPLACE     VIEW  DB.PUBLIC.MY_VIEW  (  ID  )  AS SELECT ID FROM DB.PUBLIC.CUSTOMERS",
        },
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.NOOP, change.diff


def test_view_query_still_detects_real_change():
    obj = ManifestObject(
        resource="view",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_VIEW"},
        body={"query": "SELECT ID, EMAIL FROM DB.PUBLIC.CUSTOMERS"},
    )
    client = FakeCoreObjectClient(live={
        ("view", "MY_VIEW"): {
            "query": "CREATE OR REPLACE VIEW DB.PUBLIC.MY_VIEW (ID) AS SELECT ID FROM DB.PUBLIC.CUSTOMERS",
        },
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.UPDATE
    assert "query" in change.diff


def test_query_field_not_ddl_normalized_for_non_view_resources():
    """The view-DDL unwrapping is specific to `view` — a dynamic-table's
    `query` (or any other resource's) should compare literally, not get
    the same AS-stripping treatment."""
    obj = ManifestObject(
        resource="dynamic-table",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_DT"},
        body={"query": "SELECT ID FROM DB.PUBLIC.CUSTOMERS"},
    )
    client = FakeCoreObjectClient(live={
        ("dynamic-table", "MY_DT"): {"query": "SELECT ID FROM DB.PUBLIC.CUSTOMERS"},
    })

    [change] = compute_plan([obj], client)

    assert change.action == Action.NOOP, change.diff


# --------------------------------------------------------------------- #
# Column-level +/-/~ diff rendering
# --------------------------------------------------------------------- #

def test_render_shows_added_and_removed_columns():
    obj = _table("CUSTOMERS", [
        {"name": "ID", "datatype": "NUMBER(38,0)"},
        {"name": "EMAIL", "datatype": "VARCHAR(255)"},
    ])
    client = FakeCoreObjectClient(live={
        ("table", "CUSTOMERS"): {"columns": [
            {"name": "ID", "datatype": "NUMBER(38,0)"},
            {"name": "OLD_COL", "datatype": "VARCHAR(50)"},
        ]},
    })

    [change] = compute_plan([obj], client)
    rendered = change.diff["columns"].render()

    assert "+ EMAIL VARCHAR(255)" in rendered
    assert "- OLD_COL VARCHAR(50)" in rendered


def test_render_shows_changed_column_datatype():
    obj = _table("CUSTOMERS", [{"name": "AMOUNT", "datatype": "VARCHAR(50)"}])
    client = FakeCoreObjectClient(live={
        ("table", "CUSTOMERS"): {"columns": [{"name": "AMOUNT", "datatype": "NUMBER(38,0)"}]},
    })

    [change] = compute_plan([obj], client)
    rendered = change.diff["columns"].render()

    assert rendered == "~ AMOUNT (datatype: NUMBER(38,0) -> VARCHAR(50))"


def test_render_falls_back_to_plain_line_for_scalar_fields():
    obj = ManifestObject(
        resource="warehouse",
        path_params={"name": "ETL_WH"},
        body={"warehouse_size": "MEDIUM"},
    )
    client = FakeCoreObjectClient(live={("warehouse", "ETL_WH"): {"warehouse_size": "SMALL"}})

    [change] = compute_plan([obj], client)
    rendered = change.diff["warehouse_size"].render()

    assert rendered == "'SMALL' -> 'MEDIUM'"


# --------------------------------------------------------------------- #
# Unified-diff (context-line) rendering for multi-line text fields
# --------------------------------------------------------------------- #

def test_render_multiline_field_shows_unified_diff_with_context():
    """A multi-line text field (e.g. a view's normalized query) should
    render as a line-level diff with a couple lines of context around
    each change — a Notepad++/git-style compare — not the whole old and
    new text dumped side by side."""
    live_query = "SELECT\n  ID,\n  NAME,\n  EMAIL,\n  CREATED_AT\nFROM CUSTOMERS"
    desired_query = "SELECT\n  ID,\n  NAME,\n  PHONE,\n  CREATED_AT\nFROM CUSTOMERS"

    obj = ManifestObject(
        resource="view",
        path_params={"database": "DB", "schema": "PUBLIC", "name": "MY_VIEW"},
        body={"query": desired_query},
    )
    client = FakeCoreObjectClient(live={
        ("view", "MY_VIEW"): {"query": f"CREATE OR REPLACE VIEW DB.PUBLIC.MY_VIEW AS {live_query}"},
    })

    [change] = compute_plan([obj], client)
    rendered = change.diff["query"].render()

    assert "\n" in rendered, "expected a multi-line unified diff, not a single old->new line"
    assert "-  EMAIL," in rendered
    assert "+  PHONE," in rendered
    # Context lines (unchanged) should still be present around the change
    assert "NAME," in rendered
    assert "CREATED_AT" in rendered
    # difflib's filename headers aren't meaningful here and should be stripped
    assert "--- " not in rendered
    assert "+++ " not in rendered


def test_render_single_line_string_field_does_not_use_unified_diff():
    """A short, single-line scalar (no newlines on either side) should
    stay a plain 'old -> new' line — a unified diff adds noise (hunk
    headers, etc.) for a one-line value with nothing to give context on."""
    obj = ManifestObject(resource="warehouse", path_params={"name": "WH"}, body={"warehouse_size": "MEDIUM"})
    client = FakeCoreObjectClient(live={("warehouse", "WH"): {"warehouse_size": "SMALL"}})

    [change] = compute_plan([obj], client)
    rendered = change.diff["warehouse_size"].render()

    assert rendered == "'SMALL' -> 'MEDIUM'"
    assert "@@" not in rendered