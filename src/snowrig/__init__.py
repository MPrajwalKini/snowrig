"""snowrig — a lightweight Python library for typed, idempotent Snowflake
object lifecycle management. Import it and call plan()/apply() directly
from your own scripts, pipelines, or orchestration code, instead of
shelling out to a separate CLI/IaC toolchain.

    import snowrig

    result = snowrig.plan("manifests/", profile="prod")
    for change in result:
        print(change.action, change.key, change.is_destructive)

    snowrig.apply("manifests/", profile="prod")

Both functions accept an already-open SnowflakeConnection via
`connection=`, for embedding inside code that manages its own connection
pool (Airflow, Dagster, a long-running service) — in that case snowrig
never opens or closes the connection itself, and warehouse/role are left
to whatever the caller's connection already has in session context.

The CLI (`snowrig plan` / `snowrig apply` on the command line) is a thin
wrapper around these same two functions — nothing it does isn't
available here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from snowflake.core import Root

from snowrig.config import load_profile
from snowrig.connection_bkp import connect
from snowrig.manifest.diff import (
    Action,
    FieldDiff,
    PlannedChange,
    apply_plan,
    compute_plan,
)
from snowrig.manifest.graph import DependencyError, build_apply_order
from snowrig.manifest.loader import ManifestError, load_manifest_dir
from snowrig.resources.core_client import CoreObjectClient
from snowrig.sql import SqlRunner

if TYPE_CHECKING:
    from snowflake.connector import SnowflakeConnection

__all__ = [
    "plan",
    "apply",
    "Action",
    "FieldDiff",
    "PlannedChange",
    "ManifestError",
    "DependencyError",
]

try:
    __version__ = version("snowrig")
except PackageNotFoundError:
    # Running from a source checkout without an installed/editable package
    # (e.g. tests invoked via PYTHONPATH rather than `pip install -e .`).
    __version__ = "0.0.0+unknown"


def _open_connection(profile: str) -> tuple["SnowflakeConnection", str | None, str | None]:
    prof = load_profile(profile)
    return connect(prof), prof.warehouse, prof.role


def plan(
    manifest_dir: str,
    *,
    profile: str = "default",
    connection: "SnowflakeConnection | None" = None,
) -> list[PlannedChange]:
    """Loads a manifest directory, orders it by dependency, and diffs every
    object against live Snowflake state. Doesn't change anything.

    Pass `connection=` to reuse an already-open SnowflakeConnection instead
    of opening one from a profile — snowrig won't close it in that case.
    """
    ordered = build_apply_order(load_manifest_dir(manifest_dir))

    owns_connection = connection is None
    conn = connection
    if owns_connection:
        conn, _warehouse, _role = _open_connection(profile)

    try:
        client = CoreObjectClient(Root(conn))
        return compute_plan(ordered, client)
    finally:
        if owns_connection:
            conn.close()


def apply(
    manifest_dir: str,
    *,
    profile: str = "default",
    connection: "SnowflakeConnection | None" = None,
    dry_run: bool = False,
    stop_on_error: bool = True,
    allow_destructive: bool = False,
) -> list[tuple[PlannedChange, str | None]]:
    """Applies a manifest directory to Snowflake, in dependency order.

    Destructive changes (dropping a manifest-declared column, etc.) are
    skipped unless `allow_destructive=True` — check `PlannedChange.blocked`
    on the returned results for anything that was skipped this way.

    Pass `connection=` to reuse an already-open SnowflakeConnection instead
    of opening one from a profile — snowrig won't close it in that case,
    and warehouse/role are left as whatever that connection already has in
    session context rather than being pulled from the profile.
    """
    ordered = build_apply_order(load_manifest_dir(manifest_dir))

    owns_connection = connection is None
    conn = connection
    warehouse = role = None
    if owns_connection:
        conn, warehouse, role = _open_connection(profile)

    try:
        client = CoreObjectClient(Root(conn))
        sql_runner = SqlRunner(conn)
        plan_result = compute_plan(ordered, client)
        return apply_plan(
            plan_result,
            client,
            sql_runner=sql_runner,
            warehouse=warehouse,
            role=role,
            dry_run=dry_run,
            stop_on_error=stop_on_error,
            allow_destructive=allow_destructive,
        )
    finally:
        if owns_connection:
            conn.close()