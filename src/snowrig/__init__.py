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

Calling plan()/apply() multiple times this way opens and closes a fresh
connection each call. For repeated calls — a deploy script running
plan() then apply(), an Airflow task doing several things in one run —
use snowrig.session() instead, which holds one connection open across
several calls:

    with snowrig.session(profile="prod") as s:
        s.plan("manifests/")
        s.apply("manifests/")
        s.query("SELECT CURRENT_VERSION()")

plan()/apply() are themselves just a one-call convenience wrapper around
a Session opened and closed for that single call.

The CLI (`snowrig plan` / `snowrig apply` on the command line) is a thin
wrapper around these same functions — nothing it does isn't available
here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from snowflake.core import Root

from snowrig.config import load_profile
from snowrig.connection import connect
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
    "session",
    "Session",
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


class Session:
    """A reusable Snowflake connection for calling plan()/apply()/query()
    more than once without reopening a connection each time. Not meant to
    be constructed directly — use snowrig.session(...) as a context
    manager, which returns one of these:

        with snowrig.session(profile="prod") as s:
            s.plan("manifests/")
            s.apply("manifests/")

    Pass connection= to reuse an already-open SnowflakeConnection instead
    of opening one from a profile — the session won't close it on exit,
    and warehouse/role are left as whatever that connection already has
    in session context rather than pulled from a profile.
    """

    def __init__(
        self,
        profile: str = "default",
        connection: "SnowflakeConnection | None" = None,
    ):
        self._profile = profile
        self._external_connection = connection
        self._conn: "SnowflakeConnection | None" = None
        self._owns_connection = False
        self._warehouse: str | None = None
        self._role: str | None = None

    def __enter__(self) -> "Session":
        if self._external_connection is not None:
            self._conn = self._external_connection
            self._owns_connection = False
        else:
            self._conn, self._warehouse, self._role = _open_connection(self._profile)
            self._owns_connection = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._owns_connection and self._conn is not None:
            self._conn.close()
        self._conn = None

    def _require_open(self) -> "SnowflakeConnection":
        if self._conn is None:
            raise RuntimeError(
                "Session is not open — use it as a context manager: "
                "`with snowrig.session(...) as s: ...`"
            )
        return self._conn

    def plan(self, manifest_dir: str) -> list[PlannedChange]:
        """Loads a manifest directory, orders it by dependency, and diffs
        every object against live Snowflake state. Doesn't change anything."""
        conn = self._require_open()
        ordered = build_apply_order(load_manifest_dir(manifest_dir))
        client = CoreObjectClient(Root(conn))
        return compute_plan(ordered, client)

    def apply(
        self,
        manifest_dir: str,
        *,
        dry_run: bool = False,
        stop_on_error: bool = True,
        allow_destructive: bool = False,
    ) -> list[tuple[PlannedChange, str | None]]:
        """Applies a manifest directory to Snowflake, in dependency order.
        Destructive changes are skipped unless allow_destructive=True —
        see PlannedChange.blocked on the returned results."""
        conn = self._require_open()
        ordered = build_apply_order(load_manifest_dir(manifest_dir))
        client = CoreObjectClient(Root(conn))
        sql_runner = SqlRunner(conn)
        plan_result = compute_plan(ordered, client)
        return apply_plan(
            plan_result,
            client,
            sql_runner=sql_runner,
            warehouse=self._warehouse,
            role=self._role,
            dry_run=dry_run,
            stop_on_error=stop_on_error,
            allow_destructive=allow_destructive,
        )

    def query(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> tuple[list[str], list[tuple], int]:
        """Runs a raw SQL query over this session's connection, returning
        (columns, rows, rowcount). Pass params for a parameterized query
        (driver-bound, not string-formatted) — see SqlRunner.run_query()."""
        conn = self._require_open()
        return SqlRunner(conn).run_query(sql, params)


def session(
    profile: str = "default",
    connection: "SnowflakeConnection | None" = None,
) -> Session:
    """Returns a Session for calling plan()/apply()/query() over one reused
    connection instead of a fresh one per call. Use as a context manager:

        with snowrig.session(profile="prod") as s:
            s.plan("manifests/")
            s.apply("manifests/")
    """
    return Session(profile=profile, connection=connection)


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

    Opens and closes its own connection for this one call. Calling this
    repeatedly reopens a connection each time — use snowrig.session() to
    reuse one connection across several plan()/apply()/query() calls.
    """
    with Session(profile=profile, connection=connection) as s:
        return s.plan(manifest_dir)


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

    Opens and closes its own connection for this one call. Calling this
    repeatedly reopens a connection each time — use snowrig.session() to
    reuse one connection across several plan()/apply()/query() calls.
    """
    with Session(profile=profile, connection=connection) as s:
        return s.apply(
            manifest_dir,
            dry_run=dry_run,
            stop_on_error=stop_on_error,
            allow_destructive=allow_destructive,
        )