"""Maps snowrig resource names to snowflake.core's own model classes and
tells you how to navigate Root -> ... -> the right collection for a given
object. This is deliberately a small, hand-maintained mapping rather than
codegen from a spec: snowflake.core is the source of truth now, and its
model classes are what we actually construct and pass to it, so there's
nothing left to generate — just a lookup table.

To add a new resource type: import its model class from snowflake.core and
add one entry here.
"""

from __future__ import annotations

from typing import Any

from snowflake.core.database import Database
from snowflake.core.schema import Schema
from snowflake.core.stream import Stream
from snowflake.core.table import Table
from snowflake.core.task import Task
from snowflake.core.view import View
from snowflake.core.warehouse import Warehouse

# Resources managed through snowflake.core's typed object model. Procedures,
# functions, and anything else with a real SQL body are deliberately NOT
# here — they're routed through raw `CREATE OR REPLACE` via the SQL
# connector instead (see manifest schema's `sql:` field), which is more
# robust than trying to construct their typed models generically.
RESOURCE_MODELS: dict[str, type] = {
    "database": Database,
    "schema": Schema,
    "table": Table,
    "view": View,
    "warehouse": Warehouse,
    "stream": Stream,
    "task": Task,
}


def get_collection(root: Any, resource: str, path_params: dict[str, str]) -> Any:
    if resource == "database":
        return root.databases
    if resource == "warehouse":
        return root.warehouses
    db = path_params.get("database")
    schema = path_params.get("schema")
    if resource == "schema":
        return root.databases[db].schemas
    if resource == "table":
        return root.databases[db].schemas[schema].tables
    if resource == "view":
        return root.databases[db].schemas[schema].views
    if resource == "stream":
        return root.databases[db].schemas[schema].streams
    if resource == "task":
        return root.databases[db].schemas[schema].tasks
    raise NotImplementedError(
        f"No collection mapping for resource '{resource}'. "
        f"Available: {sorted(RESOURCE_MODELS)}. Add one in core_registry.py."
    )
