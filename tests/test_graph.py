from __future__ import annotations

import pytest

from snowrig.manifest.graph import DependencyError, build_apply_order
from snowrig.manifest.schema import ManifestObject


def _db(name: str) -> ManifestObject:
    return ManifestObject(resource="database", path_params={"name": name}, body={})


def _schema(db: str, name: str) -> ManifestObject:
    return ManifestObject(resource="schema", path_params={"database": db, "name": name}, body={})


def _table(db: str, schema: str, name: str, depends_on: list[str] | None = None) -> ManifestObject:
    return ManifestObject(
        resource="table",
        path_params={"database": db, "schema": schema, "name": name},
        body={},
        depends_on=depends_on or [],
    )


def _warehouse(name: str) -> ManifestObject:
    return ManifestObject(resource="warehouse", path_params={"name": name}, body={})


def test_database_before_schema_before_table():
    objects = [
        _table("DB", "PUBLIC", "CUSTOMERS"),
        _schema("DB", "PUBLIC"),
        _db("DB"),
    ]

    ordered = build_apply_order(objects)
    kinds = [o.resource for o in ordered]

    assert kinds.index("database") < kinds.index("schema")
    assert kinds.index("schema") < kinds.index("table")


def test_account_scoped_resource_has_no_dependency():
    """A warehouse has no db/schema, so ordering relative to a table is
    unconstrained — it should not raise, and should appear in the plan."""
    objects = [_warehouse("COMPUTE_WH"), _db("DB"), _schema("DB", "PUBLIC"), _table("DB", "PUBLIC", "T")]

    ordered = build_apply_order(objects)

    assert {o.resource for o in ordered} == {"warehouse", "database", "schema", "table"}


def test_explicit_depends_on_is_respected():
    objects = [
        _db("DB"),
        _schema("DB", "PUBLIC"),
        _table("DB", "PUBLIC", "STREAM_TARGET"),
        _table("DB", "PUBLIC", "DOWNSTREAM", depends_on=["table:DB.PUBLIC.STREAM_TARGET"]),
    ]

    ordered = build_apply_order(objects)
    names = [o.path_params.get("name") for o in ordered]

    assert names.index("STREAM_TARGET") < names.index("DOWNSTREAM")


def test_depends_on_outside_manifest_is_ignored_not_an_error():
    """A dependency on something not managed by this manifest run (e.g. a
    pre-existing shared database) shouldn't block ordering."""
    objects = [
        _db("DB"),
        _schema("DB", "PUBLIC"),
        _table("DB", "PUBLIC", "T", depends_on=["table:OTHER_DB.OTHER_SCHEMA.NOT_HERE"]),
    ]

    ordered = build_apply_order(objects)

    assert len(ordered) == 3


def test_cycle_raises_dependency_error():
    objects = [
        _db("DB"),
        _schema("DB", "PUBLIC"),
        _table("DB", "PUBLIC", "A", depends_on=["table:DB.PUBLIC.B"]),
        _table("DB", "PUBLIC", "B", depends_on=["table:DB.PUBLIC.A"]),
    ]

    with pytest.raises(DependencyError):
        build_apply_order(objects)


def test_stable_order_for_independent_objects():
    """Objects with no dependency relationship keep their input order —
    important so `plan`/`apply` output doesn't reshuffle across runs."""
    objects = [_db("DB"), _schema("DB", "PUBLIC"), _table("DB", "PUBLIC", "A"), _table("DB", "PUBLIC", "B")]

    ordered = build_apply_order(objects)
    names = [o.path_params.get("name") for o in ordered if o.resource == "table"]

    assert names == ["A", "B"]