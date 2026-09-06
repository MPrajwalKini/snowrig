"""Tests for manifest/loader.py: walking a manifest directory tree and
parsing each .yaml/.yml file into a ManifestObject, including the error
paths for malformed files.

Uses pytest's tmp_path fixture to write real files to disk rather than
mocking the filesystem — load_manifest_dir's whole job is directory
walking + yaml.safe_load, so faking either would just be re-testing the
mock instead of the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowrig.manifest.loader import ManifestError, load_manifest_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_loads_a_well_formed_object(tmp_path):
    _write(
        tmp_path / "MY_DB/PUBLIC/table.customers.yaml",
        """
        resource: table
        path_params:
          database: MY_DB
          schema: PUBLIC
          name: CUSTOMERS
        body:
          columns:
            - name: ID
              datatype: NUMBER(38,0)
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.resource == "table"
    assert obj.path_params == {"database": "MY_DB", "schema": "PUBLIC", "name": "CUSTOMERS"}
    assert obj.body["columns"][0]["name"] == "ID"
    assert obj.depends_on == []
    assert obj.sql is None
    assert obj.source_path == tmp_path / "MY_DB/PUBLIC/table.customers.yaml"


def test_path_params_values_are_coerced_to_strings(tmp_path):
    """path_params is typed dict[str, str] — a bare YAML integer/bool
    value (e.g. a numeric-looking name) needs coercing, not passing
    through as an int, or downstream string formatting (ObjectKey,
    f-strings in schema.py) breaks."""
    _write(
        tmp_path / "warehouse.yaml",
        """
        resource: warehouse
        path_params:
          name: 12345
        body: {}
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.path_params["name"] == "12345"
    assert isinstance(obj.path_params["name"], str)


def test_missing_body_defaults_to_empty_dict(tmp_path):
    _write(
        tmp_path / "warehouse.yaml",
        """
        resource: warehouse
        path_params:
          name: COMPUTE_WH
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.body == {}


def test_explicit_null_body_defaults_to_empty_dict(tmp_path):
    """`body:` with nothing after it parses as None, not {} — the loader
    needs to catch that explicitly (`doc.get("body", {}) or {}`)."""
    _write(
        tmp_path / "warehouse.yaml",
        """
        resource: warehouse
        path_params:
          name: COMPUTE_WH
        body:
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.body == {}


def test_depends_on_defaults_to_empty_list(tmp_path):
    _write(
        tmp_path / "table.yaml",
        """
        resource: table
        path_params: {database: DB, schema: PUBLIC, name: T}
        body: {}
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.depends_on == []


def test_sql_field_is_loaded_when_present(tmp_path):
    _write(
        tmp_path / "procedure.yaml",
        """
        resource: procedure
        path_params: {database: DB, schema: PUBLIC, name: MY_PROC}
        body: {arguments: []}
        sql: "create or replace procedure my_proc() returns varchar language sql as $$ select 1 $$;"
        """,
    )

    [obj] = load_manifest_dir(tmp_path)

    assert obj.sql is not None
    assert "create or replace procedure" in obj.sql


def test_missing_resource_key_raises_manifest_error(tmp_path):
    bad_file = tmp_path / "broken.yaml"
    _write(
        bad_file,
        """
        path_params: {name: COMPUTE_WH}
        body: {}
        """,
    )

    with pytest.raises(ManifestError, match="missing required key 'resource'"):
        load_manifest_dir(tmp_path)


def test_missing_path_params_key_raises_manifest_error(tmp_path):
    _write(
        tmp_path / "broken.yaml",
        """
        resource: warehouse
        body: {}
        """,
    )

    with pytest.raises(ManifestError, match="missing required key 'path_params'"):
        load_manifest_dir(tmp_path)


def test_error_message_includes_offending_file_path(tmp_path):
    bad_file = tmp_path / "subdir/broken.yaml"
    _write(bad_file, "body: {}")

    with pytest.raises(ManifestError) as exc_info:
        load_manifest_dir(tmp_path)

    assert str(bad_file) in str(exc_info.value)


def test_invalid_yaml_syntax_raises_manifest_error(tmp_path):
    _write(
        tmp_path / "broken.yaml",
        """
        resource: table
        path_params: [this is not valid: yaml: syntax
        """,
    )

    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest_dir(tmp_path)


def test_empty_file_raises_manifest_error_not_attribute_error(tmp_path):
    """An empty file parses to None via yaml.safe_load — `doc = ... or {}`
    turns that into {}, which then correctly fails the 'resource' check
    rather than raising AttributeError/TypeError on a None doc."""
    _write(tmp_path / "empty.yaml", "")

    with pytest.raises(ManifestError, match="missing required key 'resource'"):
        load_manifest_dir(tmp_path)


def test_nonexistent_directory_raises_manifest_error():
    with pytest.raises(ManifestError, match="is not a directory"):
        load_manifest_dir("/no/such/path/exists")


def test_walks_nested_directories(tmp_path):
    _write(tmp_path / "DB1/PUBLIC/table.a.yaml", "resource: table\npath_params: {database: DB1, schema: PUBLIC, name: A}\n")
    _write(tmp_path / "DB2/PUBLIC/table.b.yaml", "resource: table\npath_params: {database: DB2, schema: PUBLIC, name: B}\n")
    _write(tmp_path / "account/warehouse.yaml", "resource: warehouse\npath_params: {name: WH}\n")

    objects = load_manifest_dir(tmp_path)

    assert len(objects) == 3
    names = {o.path_params["name"] for o in objects}
    assert names == {"A", "B", "WH"}


def test_non_yaml_files_are_ignored(tmp_path):
    _write(tmp_path / "table.yaml", "resource: table\npath_params: {database: DB, schema: PUBLIC, name: T}\n")
    _write(tmp_path / "README.md", "# not a manifest file")
    _write(tmp_path / "notes.txt", "scratch notes")

    objects = load_manifest_dir(tmp_path)

    assert len(objects) == 1


def test_yaml_and_yml_extensions_both_load_but_are_not_interleaved_in_sort_order(tmp_path):
    """Documents current behavior rather than asserting it's ideal: the
    loader globs *.yaml and *.yml separately (each sorted on its own),
    then concatenates the two lists — so a .yml file that would sort
    alphabetically before a .yaml file still loads after ALL .yaml files.
    Harmless today since build_apply_order re-sorts everything by
    dependency anyway, but worth knowing if you ever rely on load order
    directly."""
    _write(tmp_path / "a.yml", "resource: table\npath_params: {database: DB, schema: PUBLIC, name: A}\n")
    _write(tmp_path / "b.yaml", "resource: table\npath_params: {database: DB, schema: PUBLIC, name: B}\n")

    objects = load_manifest_dir(tmp_path)

    assert [o.path_params["name"] for o in objects] == ["B", "A"]