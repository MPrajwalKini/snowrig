"""Computes what `apply` would actually do: for each manifest object, fetch
its live definition (if any) and compare against the desired `body`. This
is deliberately a shallow, key-subset comparison rather than a full
semantic diff — snowflake.core's fetched models include many server-
computed fields (timestamps, owner, clustering stats, etc.) that aren't
part of what you declared, so we only compare the keys you actually
specified in `body`.

Objects with a raw `sql:` body (procedures, functions, anything better
expressed as CREATE OR REPLACE than a typed model) skip the live-fetch
entirely — there's no snowflake.core model to diff against — and always
get (re-)applied. CREATE OR REPLACE is idempotent at the SQL level, so
this is safe, just less informative than a real field diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from snowflake.core.exceptions import NotFoundError

from snowrig.manifest.schema import ManifestObject, ObjectKey
from snowrig.resources.core_client import CoreObjectClient
from snowrig.sql import SqlRunner


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    ERROR = "error"


@dataclass
class PlannedChange:
    obj: ManifestObject
    key: ObjectKey
    action: Action
    diff: dict[str, tuple[Any, Any]]
    error: str | None = None


def _values_match(live_value: Any, desired_value: Any) -> bool:
    """Loose equality that handles Snowflake's richer live representations
    of things you declared minimally — notably column lists, where a live
    fetch includes nullable/ordinal/etc. that your manifest never mentioned."""
    if isinstance(desired_value, list) and isinstance(live_value, list):
        if all(isinstance(d, dict) for d in desired_value) and all(
            isinstance(l, dict) for l in live_value
        ):
            if len(desired_value) != len(live_value):
                return False
            live_by_name = {item.get("name"): item for item in live_value if "name" in item}
            for desired_item in desired_value:
                name = desired_item.get("name")
                live_item = live_by_name.get(name)
                if live_item is None:
                    return False
                # Only compare the sub-fields you actually specified — a
                # live column carrying extra metadata you never declared
                # (nullable, ordinal position, ...) isn't a "change".
                for k, v in desired_item.items():
                    if str(live_item.get(k)).lower() != str(v).lower():
                        return False
            return True
    return str(live_value).lower() == str(desired_value).lower()


def _diff_fields(live: dict[str, Any], desired: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    changes = {}
    for field, desired_value in desired.items():
        live_value = live.get(field)
        if not _values_match(live_value, desired_value):
            changes[field] = (live_value, desired_value)
    return changes


def compute_plan(
    ordered_objects: list[ManifestObject], client: CoreObjectClient
) -> list[PlannedChange]:
    plan: list[PlannedChange] = []
    for obj in ordered_objects:
        key = obj.key()

        if obj.sql:
            plan.append(
                PlannedChange(
                    obj=obj, key=key, action=Action.UPDATE,
                    diff={"sql": ("<existing>", "<manifest-defined, will re-apply>")},
                )
            )
            continue

        try:
            live = client.fetch(obj.resource, obj.path_params)
        except NotFoundError:
            plan.append(PlannedChange(obj=obj, key=key, action=Action.CREATE, diff={}))
            continue
        except Exception as exc:  # noqa: BLE001
            plan.append(PlannedChange(obj=obj, key=key, action=Action.ERROR, diff={}, error=str(exc)))
            continue

        diff = _diff_fields(live, obj.body)
        action = Action.UPDATE if diff else Action.NOOP
        plan.append(PlannedChange(obj=obj, key=key, action=action, diff=diff))

    return plan


def apply_plan(
    plan: list[PlannedChange],
    client: CoreObjectClient,
    *,
    sql_runner: SqlRunner | None = None,
    warehouse: str | None = None,
    role: str | None = None,
    dry_run: bool = False,
    stop_on_error: bool = True,
) -> list[tuple[PlannedChange, str | None]]:
    """Executes every CREATE/UPDATE item: via create_or_alter() normally, or
    via raw SQL for objects with a `sql:` body. Returns (change, error) per
    attempted item, in the order applied."""
    results: list[tuple[PlannedChange, str | None]] = []

    for change in plan:
        if change.action == Action.ERROR:
            results.append((change, change.error))
            if stop_on_error:
                break
            continue
        if change.action == Action.NOOP:
            continue
        if dry_run:
            results.append((change, None))
            continue

        try:
            if change.obj.sql:
                if sql_runner is None:
                    raise RuntimeError(
                        "Object defines `sql:` but no SqlRunner was provided to apply_plan()"
                    )
                sql_runner.run(
                    change.obj.sql,
                    database=change.obj.path_params.get("database"),
                    schema=change.obj.path_params.get("schema"),
                    warehouse=warehouse,
                    role=role,
                )
            else:
                client.create_or_alter(change.obj.resource, change.obj.path_params, change.obj.body)
            results.append((change, None))
        except Exception as exc:  # noqa: BLE001
            results.append((change, str(exc)))
            if stop_on_error:
                break

    return results
