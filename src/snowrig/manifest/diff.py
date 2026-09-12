"""Computes what `apply` would actually do: for each manifest object, fetch
its live definition (if any) and compare against the desired `body`. This
is deliberately a shallow, key-subset comparison rather than a full
semantic diff — snowflake.core's fetched models include many server-
computed fields (timestamps, owner, clustering stats, etc.) that aren't
part of what you declared, so we only compare the keys you actually
specified in `body`. That's intentional for scalar fields, and it's also
applied to single nested-object fields (e.g. Stream.stream_source): only
the sub-keys the manifest declares are compared, not server-added extras
fetch() returns alongside them.

For list-of-dict fields (currently just `columns`), it's a different
story: the manifest's list is treated as *authoritative/exhaustive* for
that field, so an item present live but missing from the manifest (e.g. a
column that used to be declared and no longer is) is flagged as a
DESTRUCTIVE removal, not silently absorbed into a generic "changed" diff.
`apply_plan` refuses to execute destructive changes unless explicitly
told to via `allow_destructive=True`.

Two comparisons get extra normalization before the equality check, both
found via live smoke testing against a real account (see CHANGELOG):
  - A column's `datatype` is normalized (NUMBER == NUMBER(38,0), etc.)
    since fetch() always returns the fully-qualified form even if the
    manifest used shorthand.
  - A `view`'s `query` has its `CREATE [OR REPLACE] VIEW ... AS ` DDL
    wrapper stripped before comparing, since fetch() returns the full
    CREATE statement, not just the SELECT the manifest declares.

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


# --------------------------------------------------------------------- #
# Datatype shorthand normalization (columns)
# --------------------------------------------------------------------- #

# Not exhaustive — covers the common numeric/string/binary/temporal
# shorthands. Extend this if you hit a type Snowflake fully-qualifies
# that isn't here yet; the symptom is a column that diffs as changed on
# every single plan() even though nothing about it actually changed.
_DATATYPE_DEFAULTS: dict[str, str] = {
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
    "NVARCHAR": "VARCHAR(16777216)",
    "NVARCHAR2": "VARCHAR(16777216)",
    "CHAR": "VARCHAR(1)",
    "CHARACTER": "VARCHAR(1)",
    "NCHAR": "VARCHAR(1)",
    "BINARY": "BINARY(8388608)",
    "VARBINARY": "BINARY(8388608)",
    "TIME": "TIME(9)",
    "TIMESTAMP": "TIMESTAMP_NTZ(9)",
    "DATETIME": "TIMESTAMP_NTZ(9)",
    "TIMESTAMP_NTZ": "TIMESTAMP_NTZ(9)",
    "TIMESTAMP_LTZ": "TIMESTAMP_LTZ(9)",
    "TIMESTAMP_TZ": "TIMESTAMP_TZ(9)",
}


def _normalize_datatype(dt: Any) -> str:
    """NUMBER -> NUMBER(38,0), VARCHAR -> VARCHAR(16777216), etc. Already-
    parameterized types (anything with a '(' ) are assumed fully-qualified
    already and compared as-is."""
    s = str(dt).strip().upper()
    if "(" in s:
        return s
    return _DATATYPE_DEFAULTS.get(s, s)


_DATATYPE_PARTS_RE = re.compile(r"^([A-Z_]+)(?:\(([^)]*)\))?$")


def _parse_datatype(dt_normalized: str) -> tuple[str, tuple[int, ...]]:
    """'VARCHAR(150)' -> ('VARCHAR', (150,)); 'NUMBER(38,2)' -> ('NUMBER',
    (38, 2)); 'VARIANT' -> ('VARIANT', ()). Any parameter that isn't a
    plain integer (unexpected shape) yields no params rather than raising,
    so callers can treat that as "can't compare, don't guess"."""
    match = _DATATYPE_PARTS_RE.match(dt_normalized)
    if not match:
        return dt_normalized, ()
    family, params_str = match.group(1), match.group(2)
    if not params_str:
        return family, ()
    params: list[int] = []
    for part in params_str.split(","):
        try:
            params.append(int(part.strip()))
        except ValueError:
            return family, ()
    return family, tuple(params)


def _is_lossy_datatype_narrowing(live_dt: Any, desired_dt: Any) -> bool:
    """True if changing a column's declared type from `live_dt` to
    `desired_dt` risks data loss or an outright apply failure: switching
    type family entirely (e.g. VARCHAR -> NUMBER), shortening a VARCHAR/
    CHAR/BINARY length, or reducing a NUMBER/DECIMAL's precision or scale.
    Confirmed against a real account: Snowflake actually refuses a VARCHAR
    length reduction outright (`CREATE OR ALTER TABLE` errors with
    "reducing the byte-length of a varchar is not supported"), so the risk
    here is a hard `apply()` failure at least as often as silent
    truncation — plan() surfacing it as destructive up front, before the
    person even attempts --allow-destructive, is the whole point; whether
    Snowflake ends up truncating or just refusing depends on the specific
    types involved. Returns False (not narrowing) when the two types
    aren't comparable (different, non-numeric parameter shapes) rather
    than guessing."""
    live_norm, desired_norm = _normalize_datatype(live_dt), _normalize_datatype(desired_dt)
    if live_norm == desired_norm:
        return False
    live_family, live_params = _parse_datatype(live_norm)
    desired_family, desired_params = _parse_datatype(desired_norm)
    if live_family != desired_family:
        return True
    if not live_params or not desired_params or len(live_params) != len(desired_params):
        return False
    return any(d < l for d, l in zip(desired_params, live_params))


def _column_field_differs(key: str, live_value: Any, desired_value: Any) -> bool:
    """Per-declared-sub-field comparison for column-like list items,
    shared between _diff_list_field (change detection) and
    _render_list_diff (rendering) so the two can never disagree about
    whether something actually changed."""
    if key == "datatype":
        return _normalize_datatype(live_value) != _normalize_datatype(desired_value)
    return str(live_value).lower() != str(desired_value).lower()


# --------------------------------------------------------------------- #
# View query DDL-wrapper stripping
# --------------------------------------------------------------------- #

_VIEW_DDL_PREFIX_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+\S+\s*(?:\([^)]*\)\s*)?AS\s+",
    re.IGNORECASE | re.DOTALL,
)


def _strip_view_ddl_wrapper(raw: Any) -> str:
    """fetch() returns a view's `query` as the full CREATE VIEW ... AS
    <select> text, not just the SELECT — strip the wrapper so it can be
    compared against (and diffed against) the manifest's bare query."""
    s = str(raw)
    match = _VIEW_DDL_PREFIX_RE.match(s)
    return s[match.end():] if match else s


# --------------------------------------------------------------------- #
# List-of-dict (columns) unified-diff rendering
# --------------------------------------------------------------------- #

def _format_item(item: dict[str, Any]) -> str:
    """'STATUS VARCHAR(20)' for a single extra field, '(k=v, k2=v2)' for
    several — matches how a column with just a datatype reads naturally,
    without over-formatting columns that carry more metadata."""
    name = item.get("name", "?")
    other = {k: v for k, v in item.items() if k != "name"}
    if not other:
        return str(name)
    if len(other) == 1:
        return f"{name} {next(iter(other.values()))}"
    return f"{name} ({', '.join(f'{k}={v}' for k, v in other.items())})"


def _render_list_diff(
    live_value: Any,
    desired_value: list[dict[str, Any]],
    context: int,
) -> str:
    """Renders a list-of-dict field (columns, etc.) as a unified-diff-style
    block: +/-/~ for actual changes, unchanged items shown only within
    `context` lines of a change (git diff -U<context> semantics), longer
    unchanged runs collapsed to a single '...' marker.

    A changed item (same name, different declared sub-fields) renders as
    a removed-old-line immediately followed by an added-new-line — the
    full item on each side, not just the differing sub-field — matching
    how a PR "suggested change" diff shows a modified line: the whole old
    line struck through, the whole new line right below it.

    Alignment uses difflib.SequenceMatcher on item *names*, not just a
    flat compare — this positions removed/added/unchanged items correctly
    relative to each other even when the list has more than a couple
    items, rather than just dumping every changed item in isolation.
    """
    live_list: list[dict[str, Any]] = live_value if isinstance(live_value, list) else []
    live_by_name = {str(i["name"]): i for i in live_list if isinstance(i, dict) and "name" in i}
    desired_by_name = {str(i["name"]): i for i in desired_value if isinstance(i, dict) and "name" in i}
    live_names = [str(i["name"]) for i in live_list if isinstance(i, dict) and "name" in i]
    desired_names = [str(i["name"]) for i in desired_value if isinstance(i, dict) and "name" in i]

    matcher = difflib.SequenceMatcher(None, live_names, desired_names, autojunk=False)

    rows: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for name in live_names[i1:i2]:
                live_item, desired_item = live_by_name[name], desired_by_name[name]
                if any(
                    k != "name" and _column_field_differs(k, live_item.get(k), v)
                    for k, v in desired_item.items()
                ):
                    rows.append(("removed", f"- {_format_item(live_item)}"))
                    rows.append(("added", f"+ {_format_item(desired_item)}"))
                else:
                    rows.append(("context", f"  {_format_item(live_item)}"))
        else:
            for name in live_names[i1:i2]:
                rows.append(("removed", f"- {_format_item(live_by_name[name])}"))
            for name in desired_names[j1:j2]:
                rows.append(("added", f"+ {_format_item(desired_by_name[name])}"))

    return "\n".join(_apply_context_window(rows, context, noun="column"))


# --------------------------------------------------------------------- #
# Multi-line text (e.g. view query) unified-diff rendering
# --------------------------------------------------------------------- #

def _render_text_diff(live_value: str, desired_value: str, context: int) -> str:
    """Line-level diff for multi-line scalar text fields (a view's query,
    etc.) — same context-window collapsing as _render_list_diff, standard
    single-character +/-/space line prefixes (not difflib.unified_diff's
    own output, which carries --- / +++ / file-path headers that aren't
    meaningful here — there's no real file on either side)."""
    live_lines = live_value.splitlines()
    desired_lines = desired_value.splitlines()
    matcher = difflib.SequenceMatcher(None, live_lines, desired_lines, autojunk=False)

    rows: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in live_lines[i1:i2]:
                rows.append(("context", f" {line}"))
        else:
            for line in live_lines[i1:i2]:
                rows.append(("removed", f"-{line}"))
            for line in desired_lines[j1:j2]:
                rows.append(("added", f"+{line}"))

    return "\n".join(_apply_context_window(rows, context, noun="line"))


def _apply_context_window(rows: list[tuple[str, str]], context: int, noun: str = "column") -> list[str]:
    """Keeps every non-'context' row, plus up to `context` 'context' rows
    immediately above/below each one. Longer unchanged runs collapse to a
    single '...' line instead of being dropped silently (so it's clear
    something was omitted, not that the item list is actually shorter).

    If there are no changes at all, there's nothing to window around —
    collapsing would just hide the entire (unchanged) list behind a
    single unhelpful marker, so everything is shown plainly instead."""
    if all(status == "context" for status, _ in rows):
        return [line for _, line in rows]

    n = len(rows)
    keep = [False] * n
    for i, (status, _) in enumerate(rows):
        if status != "context":
            for j in range(max(0, i - context), min(n, i + context + 1)):
                keep[j] = True

    lines: list[str] = []
    i = 0
    while i < n:
        if keep[i]:
            lines.append(rows[i][1])
            i += 1
        else:
            j = i
            while j < n and not keep[j]:
                j += 1
            skipped = j - i
            lines.append(f"  ... ({skipped} unchanged {noun}{'s' if skipped != 1 else ''}) ...")
            i = j
    return lines


@dataclass
class FieldDiff:
    live: Any
    desired: Any
    # Names of list-items present live but absent from the manifest for
    # this field (e.g. dropped columns). Empty for non-destructive changes.
    removed_items: list[str] = field(default_factory=list)
    # "NAME (LIVE_TYPE -> DESIRED_TYPE)" for columns whose datatype change
    # could truncate or drop existing data (family change, or a same-family
    # parameter reduction) — see _is_lossy_datatype_narrowing. Also drives
    # is_destructive, alongside removed_items.
    narrowed_items: list[str] = field(default_factory=list)

    @property
    def is_destructive(self) -> bool:
        return bool(self.removed_items or self.narrowed_items)

    def render(self, context: int = 2) -> str:
        """Human-readable rendering of this field's change.

        For list-of-dict fields (columns, etc.): a unified-diff-style
        block, +/-/~ per item, with unchanged items shown only within
        `context` lines of an actual change and longer unchanged runs
        collapsed to a single '...' marker.

        For multi-line scalar text fields (either side containing a
        newline — e.g. a view's query): the same context-window idea,
        applied line-by-line, standard unified-diff single-char prefixes.

        For single-line scalar fields: a plain 'live -> desired' line —
        a unified diff would just be noise for a one-line value with
        nothing to give context on.
        """
        if isinstance(self.desired, list) and all(isinstance(x, dict) for x in self.desired):
            return _render_list_diff(self.live, self.desired, context)
        if (
            isinstance(self.desired, str)
            and isinstance(self.live, str)
            and ("\n" in self.desired or "\n" in self.live)
        ):
            return _render_text_diff(self.live, self.desired, context)
        return f"{self.live!r} -> {self.desired!r}"


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
        """e.g. 'columns: EMAIL, PHONE will be dropped; columns: LABEL will
        be narrowed (VARCHAR(150) -> VARCHAR(50), possible truncation)'"""
        parts = []
        for fname, d in self.diff.items():
            if d.removed_items:
                parts.append(f"{fname}: {', '.join(d.removed_items)} will be dropped")
            if d.narrowed_items:
                parts.append(
                    f"{fname}: {', '.join(d.narrowed_items)} "
                    f"will be narrowed (possible truncation)"
                )
        return "; ".join(parts)


def _diff_list_field(live_value: Any, desired_value: list[dict[str, Any]]) -> FieldDiff | None:
    """Compares a list-of-dict field (currently: `columns`). The manifest's
    list is authoritative: any named item present live but absent from
    `desired_value` is a destructive removal, and any named item present
    in both is compared only on the sub-fields the manifest declared. A
    datatype change classified as lossy by _is_lossy_datatype_narrowing is
    also destructive, even though the column itself isn't being dropped."""
    live_list: list[dict[str, Any]] = live_value if isinstance(live_value, list) else []
    live_by_name: dict[str, dict[str, Any]] = {
        str(item["name"]): item for item in live_list if isinstance(item, dict) and "name" in item
    }
    desired_by_name: dict[str, dict[str, Any]] = {
        str(item["name"]): item for item in desired_value if isinstance(item, dict) and "name" in item
    }

    removed: list[str] = [name for name in live_by_name if name not in desired_by_name]

    changed = False
    narrowed: list[str] = []
    for name, desired_item in desired_by_name.items():
        live_item = live_by_name.get(name)
        if live_item is None:
            changed = True  # new item — additive, not destructive
            continue
        for k, v in desired_item.items():
            if _column_field_differs(k, live_item.get(k), v):
                changed = True
                if k == "datatype" and _is_lossy_datatype_narrowing(live_item.get(k), v):
                    narrowed.append(f"{name} ({_normalize_datatype(live_item.get(k))} -> {_normalize_datatype(v)})")

    if not removed and not changed:
        return None
    return FieldDiff(live=live_value, desired=desired_value, removed_items=removed, narrowed_items=narrowed)


def _dict_values_match(live_value: Any, desired_value: dict[str, Any]) -> bool:
    """Subset comparison for a single nested-object field (e.g.
    Stream.stream_source): only the keys the manifest actually declared
    are compared, ignoring server-computed extras fetch() returns
    alongside them (database_name, schema_name, append_only, ...)."""
    if not isinstance(live_value, dict):
        return False
    return all(str(live_value.get(k)).lower() == str(v).lower() for k, v in desired_value.items())


def _values_match(live_value: Any, desired_value: Any) -> bool:
    """Loose equality for scalar/non-list/non-dict fields."""
    return str(live_value).lower() == str(desired_value).lower()


def _diff_fields(live: dict[str, Any], desired: dict[str, Any], resource: str) -> dict[str, FieldDiff]:
    changes: dict[str, FieldDiff] = {}
    for field_name, desired_value in desired.items():
        live_value = live.get(field_name)

        if isinstance(desired_value, list) and all(isinstance(d, dict) for d in desired_value):
            list_diff = _diff_list_field(live_value, desired_value)
            if list_diff is not None:
                changes[field_name] = list_diff
            continue

        if isinstance(desired_value, dict):
            if not _dict_values_match(live_value, desired_value):
                changes[field_name] = FieldDiff(live=live_value, desired=desired_value)
            continue

        if resource == "view" and field_name == "query" and isinstance(desired_value, str):
            live_value = _strip_view_ddl_wrapper(live_value)

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
            # NOT respecting stop_on_error here, deliberately: a block is an
            # intentional, expected outcome of the safety gate, not a
            # failure. `stop_on_error` exists to stop a batch when something
            # actually went wrong (an exception, a fetch that came back
            # unreadable) — it must not also mean "one blocked destructive
            # change anywhere in the plan silently prevents every other,
            # unrelated, perfectly safe change in the same apply() from
            # being attempted at all." That would make --allow-destructive's
            # default (blocked) state far more disruptive than the docs
            # promise ("the gate only affects changes that would drop
            # something"), and worse, it fails silently: the skipped
            # objects don't even appear in the returned results, so nothing
            # here or in the CLI's output would tell the person their other
            # changes never ran.
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