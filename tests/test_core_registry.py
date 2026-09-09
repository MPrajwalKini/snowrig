"""Tests for core_registry.py: RESOURCE_MODELS coverage and get_collection()
navigation. Uses a minimal fake Root shaped exactly like the real
snowflake.core Root/SchemaResource attribute names (dynamic_tables,
event_tables, pipes, sequences, stages, ...) so a rename on either side
would break these tests rather than silently mismatching at runtime.
"""

from __future__ import annotations

import pytest

from snowrig.resources.core_registry import RESOURCE_MODELS, get_collection


class _FakeSchemaResource:
    def __init__(self, marker: str):
        # One attribute per collection name get_collection() might reach for,
        # each holding a distinct marker string so assertions can tell which
        # collection actually got returned.
        self.tables = f"{marker}.tables"
        self.views = f"{marker}.views"
        self.streams = f"{marker}.streams"
        self.tasks = f"{marker}.tasks"
        self.dynamic_tables = f"{marker}.dynamic_tables"
        self.event_tables = f"{marker}.event_tables"
        self.pipes = f"{marker}.pipes"
        self.sequences = f"{marker}.sequences"
        self.stages = f"{marker}.stages"


class _FakeSchemasIndex:
    def __getitem__(self, name):
        return _FakeSchemaResource(f"schemas[{name}]")


class _FakeDbResource:
    def __init__(self):
        self.schemas = _FakeSchemasIndex()


class _FakeDatabasesIndex:
    def __getitem__(self, name):
        return _FakeDbResource()


class _FakeRoot:
    def __init__(self):
        self.databases = _FakeDatabasesIndex()
        self.warehouses = "root.warehouses"


# --------------------------------------------------------------------- #
# RESOURCE_MODELS coverage
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "resource",
    ["database", "schema", "table", "view", "warehouse", "stream", "task",
     "dynamic-table", "event-table", "pipe", "sequence", "stage"],
)
def test_resource_models_has_expected_entry(resource):
    assert resource in RESOURCE_MODELS
    assert isinstance(RESOURCE_MODELS[resource], type)


def test_resource_models_does_not_include_grants_roles_or_users():
    """Deliberate exclusion, not an oversight — see the module docstring
    and README's 'Honest positioning' section."""
    for name in ("grant", "role", "user"):
        assert name not in RESOURCE_MODELS


# --------------------------------------------------------------------- #
# get_collection — account-scoped resources
# --------------------------------------------------------------------- #

def test_database_collection_is_root_databases():
    root = _FakeRoot()
    assert get_collection(root, "database", {}) is root.databases


def test_warehouse_collection_is_root_warehouses():
    root = _FakeRoot()
    assert get_collection(root, "warehouse", {}) == "root.warehouses"


# --------------------------------------------------------------------- #
# get_collection — schema-scoped resources, existing types
# --------------------------------------------------------------------- #

def test_schema_collection_navigates_to_database_schemas():
    root = _FakeRoot()
    result = get_collection(root, "schema", {"database": "DB"})
    assert isinstance(result, _FakeSchemasIndex)


@pytest.mark.parametrize(
    "resource,expected_suffix",
    [
        ("table", "tables"),
        ("view", "views"),
        ("stream", "streams"),
        ("task", "tasks"),
    ],
)
def test_existing_schema_scoped_resources_navigate_correctly(resource, expected_suffix):
    root = _FakeRoot()
    result = get_collection(root, resource, {"database": "DB", "schema": "PUBLIC"})
    assert result == f"schemas[PUBLIC].{expected_suffix}"


# --------------------------------------------------------------------- #
# get_collection — newly wired schema-scoped resources
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "resource,expected_suffix",
    [
        ("dynamic-table", "dynamic_tables"),
        ("event-table", "event_tables"),
        ("pipe", "pipes"),
        ("sequence", "sequences"),
        ("stage", "stages"),
    ],
)
def test_new_schema_scoped_resources_navigate_correctly(resource, expected_suffix):
    root = _FakeRoot()
    result = get_collection(root, resource, {"database": "DB", "schema": "PUBLIC"})
    assert result == f"schemas[PUBLIC].{expected_suffix}"


def test_unknown_resource_raises_with_helpful_message():
    root = _FakeRoot()
    with pytest.raises(NotImplementedError, match="No collection mapping for resource 'nonsense'"):
        get_collection(root, "nonsense", {})