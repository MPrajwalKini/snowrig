"""Computes what `apply` would actually do: for each manifest object, fetch
its live definition (if any) and compare against the desired `body`. This
is deliberately a shallow, key-subset comparison rather than a full
semantic diff — snowflake.core's fetched models include many server-
computed fields (timestamps, owner, clustering stats, etc.) that aren't
part of what you declared, so we only compare the keys you actually
specified in `body`. That's intentional for scalar fields.

For list-of-dict fields (currently just `columns`), it's a different
story: the manifest's list is treated as *authoritative/exhaustive* for
that field, so an item present live but missing from the manifest (e.g. a
column that used to be declared and no longer is) is flagged as a
DESTRUCTIVE removal, not silently absorbed into a generic "changed" diff.
`apply_plan` refuses to execute destructive changes unless explicitly
told to via `allow_destructive=True`.

Objects with a raw `sql:` body (procedures, functions, anything better
expressed as CREATE OR REPLACE than a typed model) skip the live-fetch
entirely — there's no snowflake.core model to diff against — and always
get (re-)applied. CREATE OR REPLACE is idempotent at the SQL level, so
this is safe, just less informative than a real field diff.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from snowflake.core.exceptions import NotFoundError

from snowrig.manifest.schema import ManifestObject, ObjectKey
from snowrig.resources.core_client import CoreObjectClient, describe_error
from snowrig.sql import SqlRunner


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    ERROR = "error"


@dataclass
class FieldDiff:
    live: Any
    desired: Any
    # Names of list-items present live but absent from the manifest for
    # this field (e.g. dropped columns). Empty for non-destructive changes.
    removed_items: list[str] = field(default_factory=list)
    # Names of list-items present in the manifest but not live yet (e.g. a
    # new column being added alongside existing ones on an UPDATE).
    added_items: list[str] = field(default_factory=list)
    # item name -> {subfield_name: (live_value, desired_value)}, for items
    # present on both sides but with one or more sub-fields differing
    # (e.g. a column whose datatype changed).
    changed_items: dict[str, dict[str, tuple[Any, Any]]] = field(default_factory=dict)

    @property
    def is_destructive(self) -> bool:
        return bool(self.removed_items)

    def render(self) -> str:
        """Human-readable summary of this field's change. Three shapes,
        chosen by what the field actually is:
          - list-of-dict fields (columns): git-style '+ NAME TYPE' /
            '- NAME TYPE' / '~ NAME (sub: old -> new)', one entry per item.
          - multi-line string fields (a view's query, or anything else
            long enough to need it): a unified diff with a couple lines
            of context around each change, same idea as `git diff` or a
            Notepad++ compare — not the whole text dumped twice.
          - everything else (short scalars): a plain 'old -> new' line.
        """
        if not (self.added_items or self.removed_items or self.changed_items):
            if isinstance(self.live, str) and isinstance(self.desired, str) and self.live != self.desired:
                text_diff = _unified_text_diff(self.live, self.desired)
                if text_diff is not None:
                    return text_diff
            return f"{self.live!r} -> {self.desired!r}"

        def _find(items: Any, name: str) -> dict[str, Any]:
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("name") == name:
                        return item
            return {}

        lines: list[str] = []
        for name in self.added_items:
            item = _find(self.desired, name)
            detail = item.get("datatype", "")
            lines.append(f"+ {name}{f' {detail}' if detail else ''}")
        for name in self.removed_items:
            item = _find(self.live, name)
            detail = item.get("datatype", "")
            lines.append(f"- {name}{f' {detail}' if detail else ''}")
        for name, subfields in self.changed_items.items():
            detail = ", ".join(f"{k}: {old} -> {new}" for k, (old, new) in subfields.items())
            lines.append(f"~ {name} ({detail})")
        return "; ".join(lines)


def _unified_text_diff(live: str, desired: str, context: int = 2) -> str | None:
    """A Notepad++/git-style line-level diff with `context` lines of
    unchanged surrounding text on either side of each change, rather than
    dumping the entire old and new text. Returns None if the text isn't
    multi-line — a single-line scalar reads better as a plain
    'old -> new' line, which is what the caller falls back to."""
    if "\n" not in live and "\n" not in desired:
        return None
    live_lines = live.splitlines()
    desired_lines = desired.splitlines()
    hunks = list(difflib.unified_diff(live_lines, desired_lines, lineterm="", n=context))
    # Drop the '--- '/'+++ ' filename header lines difflib always emits
    # first — meaningless here since there's no real file on either side.
    body = [ln for ln in hunks if not (ln.startswith("--- ") or ln.startswith("+++ "))]
    return "\n".join(body) if body else None


@dataclass
class PlannedChange:
    obj: ManifestObject
    key: ObjectKey
    action: Action
    diff: dict[str, FieldDiff]
    error: str | None = None
    blocked: str | None = None  # set by apply_plan if a destructive change was skipped

    @property
    def is_destructive(self) -> bool:
        return any(d.is_destructive for d in self.diff.values())

    def destructive_summary(self) -> str:
        """e.g. 'columns: EMAIL, PHONE will be dropped'"""
        parts = []
        for fname, d in self.diff.items():
            if d.removed_items:
                parts.append(f"{fname}: {', '.join(d.removed_items)} will be dropped")
        return "; ".join(parts)


def _diff_list_field(live_value: Any, desired_value: list[dict[str, Any]]) -> FieldDiff | None:
    """Compares a list-of-dict field (currently: `columns`). The manifest's
    list is authoritative: any named item present live but absent from
    `desired_value` is a destructive removal, and any named item present
    in both is compared only on the sub-fields the manifest declared."""
    live_list: list[dict[str, Any]] = live_value if isinstance(live_value, list) else []
    live_by_name: dict[str, dict[str, Any]] = {
        str(item["name"]): item for item in live_list if isinstance(item, dict) and "name" in item
    }
    desired_by_name: dict[str, dict[str, Any]] = {
        str(item["name"]): item for item in desired_value if isinstance(item, dict) and "name" in item
    }

    removed: list[str] = [name for name in live_by_name if name not in desired_by_name]
    added: list[str] = [name for name in desired_by_name if name not in live_by_name]

    changed_items: dict[str, dict[str, tuple[Any, Any]]] = {}
    for name, desired_item in desired_by_name.items():
        live_item = live_by_name.get(name)
        if live_item is None:
            continue  # captured in `added` above
        item_changes: dict[str, tuple[Any, Any]] = {}
        for k, v in desired_item.items():
            live_v = live_item.get(k)
            if k == "datatype":
                if _normalize_type(live_v) != _normalize_type(v):
                    item_changes[k] = (live_v, v)
            elif not _values_match(live_v, v):
                item_changes[k] = (live_v, v)
        if item_changes:
            changed_items[name] = item_changes

    if not removed and not added and not changed_items:
        return None
    return FieldDiff(
        live=live_value, desired=desired_value,
        removed_items=removed, added_items=added, changed_items=changed_items,
    )


# Base Snowflake type names mapped to the canonical form `fetch()` returns
# them as, so a manifest can write the short form without permanently
# diffing against Snowflake's fully-qualified precision/scale/length.
# Best-effort, not exhaustive — extend as new mismatches surface.
_TYPE_CANONICAL = {
    "NUMBER": "NUMBER(38,0)",
    "DECIMAL": "NUMBER(38,0)",
    "NUMERIC": "NUMBER(38,0)",
    "INT": "NUMBER(38,0)",
    "INTEGER": "NUMBER(38,0)",
    "BIGINT": "NUMBER(38,0)",
    "SMALLINT": "NUMBER(38,0)",
    "TINYINT": "NUMBER(38,0)",
    "BYTEINT": "NUMBER(38,0)",
    "VARCHAR": "VARCHAR(16777216)",
    "STRING": "VARCHAR(16777216)",
    "TEXT": "VARCHAR(16777216)",
    "CHAR": "VARCHAR(1)",
    "CHARACTER": "VARCHAR(1)",
}


def _normalize_type(t: Any) -> str:
    """Canonicalizes a Snowflake column datatype string for comparison —
    e.g. 'NUMBER' and 'NUMBER(38,0)' are the same type to Snowflake, but
    fetch() always returns the fully-qualified form, so a manifest using
    the short form would otherwise diff as changed on every plan/apply."""
    s = str(t).strip().upper()
    return _TYPE_CANONICAL.get(s, s)


def _values_match(live_value: Any, desired_value: Any) -> bool:
    """Loose equality for scalar/non-list fields. A dict compares as a
    declared subset — recursively — so a nested object field (e.g.
    Stream.stream_source) only diffs on the keys the manifest actually
    declared, not server-computed extras fetch() returns alongside them."""
    if isinstance(desired_value, dict):
        if not isinstance(live_value, dict):
            return False
        return all(_values_match(live_value.get(k), v) for k, v in desired_value.items())
    return str(live_value).lower() == str(desired_value).lower()


def _normalize_whitespace(s: str) -> str:
    return " ".join(s.split())


# Matches up through the first top-level "AS" that follows "VIEW" in the
# DDL text fetch() returns for a view — i.e. the boundary between the
# "CREATE [OR REPLACE] VIEW <name> [(col, ...)]" header and the actual
# query. Non-greedy so it stops at that first AS rather than one inside
# the query body itself.
_VIEW_DDL_HEADER_RE = re.compile(r"\bVIEW\b.*?\bAS\b\s*", re.IGNORECASE | re.DOTALL)


def _extract_view_query(live_ddl: str) -> str:
    """fetch() returns a view's `query` field as the full CREATE VIEW DDL
    text, not just the SELECT — extract everything after the header so it
    can be compared against the manifest's bare query. Falls back to the
    raw string if the expected header shape isn't found."""
    match = _VIEW_DDL_HEADER_RE.search(live_ddl)
    if not match:
        return live_ddl.strip()
    return live_ddl[match.end():].strip()


def _diff_fields(live: dict[str, Any], desired: dict[str, Any], resource: str | None = None) -> dict[str, FieldDiff]:
    changes: dict[str, FieldDiff] = {}
    for field_name, desired_value in desired.items():
        live_value = live.get(field_name)

        if isinstance(desired_value, list) and all(isinstance(d, dict) for d in desired_value):
            list_diff = _diff_list_field(live_value, desired_value)
            if list_diff is not None:
                changes[field_name] = list_diff
            continue

        if resource == "view" and field_name == "query" and isinstance(live_value, str) and isinstance(desired_value, str):
            live_extracted = _extract_view_query(live_value)
            live_cmp = _normalize_whitespace(live_extracted)
            desired_cmp = _normalize_whitespace(desired_value)
            if live_cmp.lower() != desired_cmp.lower():
                changes[field_name] = FieldDiff(live=live_extracted, desired=desired_value)
            continue

        if not _values_match(live_value, desired_value):
            changes[field_name] = FieldDiff(live=live_value, desired=desired_value)

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
                    diff={"sql": FieldDiff(live="<existing>", desired="<manifest-defined, will re-apply>")},
                )
            )
            continue

        try:
            live = client.fetch(obj.resource, obj.path_params)
        except NotFoundError:
            plan.append(PlannedChange(obj=obj, key=key, action=Action.CREATE, diff={}))
            continue
        except Exception as exc:  # noqa: BLE001
            plan.append(
                PlannedChange(obj=obj, key=key, action=Action.ERROR, diff={}, error=describe_error(exc))
            )
            continue

        diff = _diff_fields(live, obj.body, resource=obj.resource)
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
    allow_destructive: bool = False,
) -> list[tuple[PlannedChange, str | None]]:
    """Executes every CREATE/UPDATE item: via create_or_alter() normally, or
    via raw SQL for objects with a `sql:` body. Returns (change, error) per
    attempted item, in the order applied.

    Destructive changes (a manifest that drops a previously-declared column,
    etc.) are skipped and reported via `change.blocked` unless
    `allow_destructive=True` is passed explicitly."""
    results: list[tuple[PlannedChange, str | None]] = []

    for change in plan:
        if change.action == Action.ERROR:
            results.append((change, change.error))
            if stop_on_error:
                break
            continue
        if change.action == Action.NOOP:
            continue

        if change.is_destructive and not allow_destructive:
            change.blocked = (
                f"Skipped — destructive change requires --allow-destructive: "
                f"{change.destructive_summary()}"
            )
            results.append((change, change.blocked))
            if stop_on_error:
                break
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
            results.append((change, describe_error(exc)))
            if stop_on_error:
                break

    return results