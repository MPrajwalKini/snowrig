"""Tests for the narrow field-coercion logic in resources/core_client.py
(Task.schedule, Stream.stream_source — see CONTRIBUTING.md) and for
CoreObjectClient's fetch/create_or_alter/delete routing.

Coercion is tested against the real snowflake.core model classes (Cron,
StreamSourceTable, timedelta) rather than fakes, since these are pure
data-shape checks and the whole point is catching a real snowflake.core
validation break. CoreObjectClient itself is tested against a fake Root
that mimics the collection-navigation shape get_collection() expects, so
these tests don't depend on live Snowflake or on every field a real
Stream/Task/Database model happens to require.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from snowflake.core.stream import Stream, StreamSourceTable
from snowflake.core.dynamic_table import UserDefinedLag
from snowflake.core.task import Cron, Task

from snowrig.resources.core_client import (
    CoreObjectClient,
    _coerce_dynamic_table_body,
    _coerce_stream_body,
    _coerce_task_body,
    _reorder_new_columns_to_end,
    describe_error,
)


# --------------------------------------------------------------------- #
# _coerce_stream_body
# --------------------------------------------------------------------- #

def test_stream_source_dict_is_coerced_to_concrete_subclass():
    body = {"stream_source": {"name": "CUSTOMERS"}}

    coerced = _coerce_stream_body(body)

    assert isinstance(coerced["stream_source"], StreamSourceTable)
    assert coerced["stream_source"].name == "CUSTOMERS"


def test_stream_body_without_stream_source_is_unchanged():
    body = {"comment": "no source here"}

    assert _coerce_stream_body(body) == body


def test_stream_source_already_concrete_is_left_alone():
    """If something upstream already built a StreamSource subclass (not a
    plain dict), the coercer shouldn't touch it."""
    source = StreamSourceTable(name="CUSTOMERS")
    body = {"stream_source": source}

    coerced = _coerce_stream_body(body)

    assert coerced["stream_source"] is source


# --------------------------------------------------------------------- #
# _coerce_dynamic_table_body
# --------------------------------------------------------------------- #

def test_dynamic_table_target_lag_dict_is_coerced_to_user_defined_lag():
    """DynamicTable.target_lag is a real object (UserDefinedLag), not a
    plain dict — the same class of problem _coerce_stream_body solves for
    Stream.stream_source. A bare dict passes local validation but fails
    server-side with an opaque, bodyless 400 (caught by the smoke test's
    dynamic-table round-trip)."""
    body = {"target_lag": {"seconds": 3600}, "warehouse": "WH", "query": "SELECT 1"}

    coerced = _coerce_dynamic_table_body(body)

    assert isinstance(coerced["target_lag"], UserDefinedLag)
    assert coerced["target_lag"].seconds == 3600
    assert coerced["warehouse"] == "WH"  # untouched


def test_dynamic_table_body_without_target_lag_is_unchanged():
    body = {"warehouse": "WH", "query": "SELECT 1"}

    assert _coerce_dynamic_table_body(body) == body


def test_dynamic_table_target_lag_already_concrete_is_left_alone():
    lag = UserDefinedLag(seconds=120)
    body = {"target_lag": lag}

    coerced = _coerce_dynamic_table_body(body)

    assert coerced["target_lag"] is lag


# --------------------------------------------------------------------- #
# _coerce_task_body
# --------------------------------------------------------------------- #

def test_task_schedule_cron_shape_becomes_cron_object():
    body = {"schedule": {"cron": "*/5 * * * *", "timezone": "America/New_York"}}

    coerced = _coerce_task_body(body)

    assert isinstance(coerced["schedule"], Cron)
    assert coerced["schedule"].expr == "*/5 * * * *"
    assert coerced["schedule"].timezone == "America/New_York"


def test_task_schedule_cron_defaults_timezone_to_utc():
    body = {"schedule": {"cron": "0 0 * * *"}}

    coerced = _coerce_task_body(body)

    assert coerced["schedule"].timezone == "UTC"


def test_task_schedule_timedelta_shape_becomes_timedelta():
    body = {"schedule": {"minutes": 5}}

    coerced = _coerce_task_body(body)

    assert coerced["schedule"] == timedelta(minutes=5)


def test_task_schedule_timedelta_shape_accepts_multiple_kwargs():
    body = {"schedule": {"hours": 1, "minutes": 30}}

    coerced = _coerce_task_body(body)

    assert coerced["schedule"] == timedelta(hours=1, minutes=30)


def test_task_schedule_invalid_keys_raise_immediately():
    body = {"schedule": {"not_a_real_key": 5}}

    with pytest.raises(ValueError, match="task.schedule must be either"):
        _coerce_task_body(body)


def test_task_schedule_mixed_cron_and_timedelta_keys_prefers_cron():
    """`cron` in the keys always wins — matches the function's `if "cron"
    in keys` check taking priority over the timedelta-subset check."""
    body = {"schedule": {"cron": "*/5 * * * *", "minutes": 5}}

    coerced = _coerce_task_body(body)

    assert isinstance(coerced["schedule"], Cron)


def test_task_body_without_schedule_is_unchanged():
    body = {"warehouse": "COMPUTE_WH", "definition": "CALL foo()"}

    assert _coerce_task_body(body) == body


def test_task_schedule_non_dict_is_left_alone():
    """A schedule that's already a Cron/timedelta object (not a raw dict)
    passes through untouched — only plain dicts from YAML get coerced."""
    body = {"schedule": timedelta(minutes=5)}

    assert _coerce_task_body(body) is body


# --------------------------------------------------------------------- #
# describe_error
# --------------------------------------------------------------------- #

class _FakeHttpResp:
    def __init__(self, data):
        self.data = data


class _FakeApiError(Exception):
    def __init__(self, message, http_resp=None):
        super().__init__(message)
        self.http_resp = http_resp


def test_describe_error_includes_response_body_when_present():
    exc = _FakeApiError("(400) Reason: Bad Request", http_resp=_FakeHttpResp("column EMAIL not found"))

    result = describe_error(exc)

    assert "(400) Reason: Bad Request" in result
    assert "column EMAIL not found" in result


def test_describe_error_decodes_bytes_body():
    exc = _FakeApiError("(400) Reason: Bad Request", http_resp=_FakeHttpResp(b"bad request body"))

    result = describe_error(exc)

    assert "bad request body" in result


def test_describe_error_falls_back_to_str_when_no_http_resp():
    exc = RuntimeError("plain old error")

    assert describe_error(exc) == "plain old error"


def test_describe_error_falls_back_when_http_resp_has_no_body():
    exc = _FakeApiError("(400) Reason: Bad Request", http_resp=_FakeHttpResp(None))

    assert describe_error(exc) == "(400) Reason: Bad Request"


# --------------------------------------------------------------------- #
# CoreObjectClient
# --------------------------------------------------------------------- #

class _FakeItem:
    """Stands in for collection[name] — a resource reference object."""

    def __init__(self, existing_model=None, *, supports_create_or_alter=True):
        self._existing_model = existing_model
        self.create_or_alter_calls: list[Any] = []
        self.dropped = False
        if supports_create_or_alter:
            self.create_or_alter = self._create_or_alter  # type: ignore[assignment]

    def _create_or_alter(self, model):
        self.create_or_alter_calls.append(model)

    def fetch(self):
        if self._existing_model is None:
            from snowflake.core.exceptions import NotFoundError

            raise NotFoundError("not found")
        return self._existing_model

    def drop(self):
        self.dropped = True


class _FakeCollection:
    def __init__(self, item: _FakeItem):
        self._item = item
        self.created: list[tuple[Any, Any]] = []

    def __getitem__(self, name):
        return self._item

    def create(self, model, mode):
        self.created.append((model, mode))


class _FakeSchemaScope:
    def __init__(self, collection: _FakeCollection):
        self.streams = collection
        self.tasks = collection
        self.tables = collection


class _FakeSchemasIndex:
    """Stands in for `root.databases[db].schemas` — indexable by schema name."""

    def __init__(self, schema_scope: _FakeSchemaScope):
        self._schema_scope = schema_scope

    def __getitem__(self, name):
        return self._schema_scope


class _FakeDbScope:
    def __init__(self, schema_scope: _FakeSchemaScope):
        self.schemas = _FakeSchemasIndex(schema_scope)


class _FakeRoot:
    def __init__(self, collection: _FakeCollection):
        self.databases = {"DB": _FakeDbScope(_FakeSchemaScope(collection))}


def test_create_or_alter_applies_stream_coercion_before_constructing_model():
    item = _FakeItem()
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.create_or_alter(
        "stream",
        {"database": "DB", "schema": "PUBLIC", "name": "CUSTOMERS_STREAM"},
        {"stream_source": {"name": "CUSTOMERS"}},
    )

    [model] = item.create_or_alter_calls
    assert isinstance(model, Stream)
    assert isinstance(model.stream_source, StreamSourceTable)


def test_create_or_alter_applies_task_coercion_before_constructing_model():
    item = _FakeItem()
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.create_or_alter(
        "task",
        {"database": "DB", "schema": "PUBLIC", "name": "PROCESS_CHANGES"},
        {"warehouse": "COMPUTE_WH", "definition": "CALL foo()", "schedule": {"minutes": 5}},
    )

    [model] = item.create_or_alter_calls
    assert isinstance(model, Task)
    assert model.schedule == timedelta(minutes=5)


def test_create_or_alter_falls_back_to_collection_create_when_no_create_or_alter():
    """Mirrors procedures/functions-shaped resources — anything whose item
    doesn't expose create_or_alter goes through collection.create(mode=or_replace)."""
    from snowflake.core import CreateMode

    item = _FakeItem(supports_create_or_alter=False)
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.create_or_alter(
        "task",
        {"database": "DB", "schema": "PUBLIC", "name": "PROCESS_CHANGES"},
        {"warehouse": "COMPUTE_WH", "definition": "CALL foo()"},
    )

    [(model, mode)] = collection.created
    assert isinstance(model, Task)
    assert mode == CreateMode.or_replace


def test_exists_returns_false_on_not_found():
    item = _FakeItem(existing_model=None)
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    assert client.exists("task", {"database": "DB", "schema": "PUBLIC", "name": "MISSING"}) is False


def test_exists_returns_true_when_fetch_succeeds():
    existing = Task(name="EXISTS", warehouse="WH", definition="CALL foo()")
    item = _FakeItem(existing_model=existing)
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    assert client.exists("task", {"database": "DB", "schema": "PUBLIC", "name": "EXISTS"}) is True


# --------------------------------------------------------------------- #
# _reorder_new_columns_to_end
# --------------------------------------------------------------------- #

def test_reorder_leaves_columns_alone_when_nothing_is_new():
    live_names = ["A", "B", "C"]
    desired = [{"name": "A"}, {"name": "B", "datatype": "VARCHAR(99)"}, {"name": "C"}]

    assert _reorder_new_columns_to_end(live_names, desired) == desired


def test_reorder_moves_a_mid_list_new_column_to_the_end():
    """Mirrors the real-world failure: a manifest that declares a new column
    in the same list position as a column it's replacing must still send
    that new column *last* to CREATE OR ALTER TABLE, since Snowflake only
    accepts new columns at the end of the column list."""
    live_names = ["A", "B", "C", "D"]
    desired = [{"name": "A"}, {"name": "B"}, {"name": "NEW"}, {"name": "D"}]  # C dropped, NEW added mid-list

    result = _reorder_new_columns_to_end(live_names, desired)

    assert [c["name"] for c in result] == ["A", "B", "D", "NEW"]


def test_reorder_preserves_live_relative_order_even_if_manifest_order_differs():
    """Existing columns keep the *live* table's order, not the manifest's —
    only the position of genuinely new columns is touched."""
    live_names = ["A", "B", "C"]
    desired = [{"name": "C"}, {"name": "A"}, {"name": "B"}, {"name": "NEW"}]

    result = _reorder_new_columns_to_end(live_names, desired)

    assert [c["name"] for c in result] == ["A", "B", "C", "NEW"]


def test_reorder_appends_multiple_new_columns_in_manifest_order():
    live_names = ["A"]
    desired = [{"name": "NEW1"}, {"name": "A"}, {"name": "NEW2"}]

    result = _reorder_new_columns_to_end(live_names, desired)

    assert [c["name"] for c in result] == ["A", "NEW1", "NEW2"]


def test_reorder_with_no_live_columns_leaves_manifest_order_as_is():
    """A brand-new table: every declared column is 'new', so there's nothing
    to reorder relative to."""
    desired = [{"name": "B"}, {"name": "A"}]

    assert _reorder_new_columns_to_end([], desired) == desired


# --------------------------------------------------------------------- #
# CoreObjectClient.create_or_alter — table column reordering
# --------------------------------------------------------------------- #

class _FakeExistingTable:
    """Stands in for the model returned by item.fetch() — only `.columns`,
    a list of objects exposing `.name`, is read by the reordering logic."""

    def __init__(self, column_names: list[str]):
        self.columns = [_SimpleNamespace(name=n) for n in column_names]


class _SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_create_or_alter_reorders_new_table_columns_to_the_end():
    from snowflake.core.table import Table

    existing = _FakeExistingTable(["A", "B", "C"])
    item = _FakeItem(existing_model=existing)
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.create_or_alter(
        "table",
        {"database": "DB", "schema": "PUBLIC", "name": "T"},
        {
            "columns": [
                {"name": "A", "datatype": "VARCHAR(50)"},
                {"name": "NEW", "datatype": "VARCHAR(20)"},  # B dropped, NEW mid-list
                {"name": "C", "datatype": "VARCHAR(50)"},
            ]
        },
    )

    [model] = item.create_or_alter_calls
    assert isinstance(model, Table)
    assert [c.name for c in model.columns] == ["A", "C", "NEW"]


def test_create_or_alter_skips_reorder_for_a_brand_new_table():
    from snowflake.core.table import Table

    item = _FakeItem(existing_model=None)  # fetch() raises NotFoundError
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.create_or_alter(
        "table",
        {"database": "DB", "schema": "PUBLIC", "name": "T"},
        {
            "columns": [
                {"name": "B", "datatype": "VARCHAR(50)"},
                {"name": "A", "datatype": "VARCHAR(50)"},
            ]
        },
    )

    [model] = item.create_or_alter_calls
    assert isinstance(model, Table)
    assert [c.name for c in model.columns] == ["B", "A"]


def test_delete_drops_the_item():
    item = _FakeItem()
    collection = _FakeCollection(item)
    root = _FakeRoot(collection)
    client = CoreObjectClient(root)

    client.delete("task", {"database": "DB", "schema": "PUBLIC", "name": "PROCESS_CHANGES"})

    assert item.dropped is True