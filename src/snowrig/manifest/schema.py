"""The unit of desired state: one Snowflake object, as declared in one YAML
file under a manifest directory tree.

Convention (mirrors Snowflake's own namespacing):

    manifests/
      account/
        warehouse.compute_wh.yaml     # scope=account, no db/schema
        role.my_role.yaml
      MY_DB/
        database.yaml                  # defines MY_DB itself
        PUBLIC/
          schema.yaml                  # defines PUBLIC itself
          table.customers.yaml
          view.customer_summary.yaml
          procedure.sql_api_stored_proc.yaml
          task.daily_refresh.yaml

Each file:

    resource: table              # must match a resource in the spec registry
    path_params:                 # fills the REST path template
      database: MY_DB
      schema: PUBLIC
      name: CUSTOMERS
    body:                        # passed through ~verbatim to create_or_alter
      columns: [...]
    depends_on: []                # optional extra deps beyond the implicit
                                   # database/schema hierarchy, e.g. a task
                                   # that reads a stream: ["stream:MY_DB.PUBLIC.MY_STREAM"]

`body` is intentionally a passthrough dict rather than a validated model per
resource type — Snowflake's own object schemas vary too much by type to be
worth hand-modeling before the tool has real mileage. SQL bodies (procedures,
tasks, UDFs) go inline inside `body` as plain strings, same shape as
Snowflake's own SQL API payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Resources here take {database, schema, name} path params and depend on
# their schema. Anything not listed falls back to SCOPE_ACCOUNT (see below)
# unless it's "schema" or "database" themselves (handled specially).
SCHEMA_SCOPED_RESOURCES = {
    "table", "view", "procedure", "function", "task", "stream",
    "dynamic-table", "event-table", "sequence", "pipe", "stage",
}

# Resources identified by name + argument signature (they can be overloaded)
# rather than a plain name. Snowflake's REST paths use {nameWithArgs} for
# these, e.g. "MY_PROC(A NUMBER, B VARCHAR)" — and correspondingly neither
# supports a PUT create-or-alter; only POST with a createMode=orReplace
# query param behaves equivalently (see resources/object_api.py).
OVERLOADABLE_RESOURCES = {"procedure", "function"}

SCOPE_ACCOUNT = "account"      # no database/schema — e.g. warehouse, role
SCOPE_DATABASE = "database"    # depends on a database only — e.g. schema
SCOPE_SCHEMA = "schema"        # depends on database+schema


@dataclass(frozen=True)
class ObjectKey:
    """Uniquely identifies a manifest object for dependency resolution."""

    resource: str
    qualified_name: str  # e.g. "MY_DB.PUBLIC.CUSTOMERS" or "COMPUTE_WH"

    def __str__(self) -> str:
        return f"{self.resource}:{self.qualified_name}"


@dataclass
class ManifestObject:
    resource: str
    path_params: dict[str, str]
    body: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    sql: str | None = None
    source_path: Path | None = None

    def scope(self) -> str:
        if self.resource == "database":
            return SCOPE_ACCOUNT
        if self.resource == "schema":
            return SCOPE_DATABASE
        if self.resource in SCHEMA_SCOPED_RESOURCES:
            return SCOPE_SCHEMA
        return SCOPE_ACCOUNT

    def key(self) -> ObjectKey:
        scope = self.scope()
        if scope == SCOPE_ACCOUNT:
            name = self.path_params.get("name", "")
        elif scope == SCOPE_DATABASE:
            name = f"{self.path_params['database']}.{self.path_params['name']}"
        else:
            name = (
                f"{self.path_params['database']}."
                f"{self.path_params['schema']}.{self.path_params['name']}"
            )
            if self.resource in OVERLOADABLE_RESOURCES:
                name = f"{name}{self._arg_signature_suffix()}"
        return ObjectKey(resource=self.resource, qualified_name=name)

    def _arg_signature_suffix(self) -> str:
        """Renders body['arguments'] as Snowflake's '(type, type)' signature —
        overload resolution in DESCRIBE/ALTER/DROP PROCEDURE and the REST
        nameWithArgs path param both key off argument TYPES only, not names.
        Used by key() to disambiguate overloads in the dependency graph."""
        args = self.body.get("arguments", [])
        parts = [a["datatype"] for a in args]
        return f"({', '.join(parts)})"

    def implicit_dependency(self) -> ObjectKey | None:
        """The database/schema this object lives in, if any."""
        scope = self.scope()
        if scope == SCOPE_ACCOUNT:
            return None
        if scope == SCOPE_DATABASE:
            return ObjectKey(resource="database", qualified_name=self.path_params["database"])
        return ObjectKey(
            resource="schema",
            qualified_name=f"{self.path_params['database']}.{self.path_params['schema']}",
        )