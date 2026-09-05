"""Builds a generic resource registry from Snowflake's official OpenAPI
specs (vendored under /specs, sourced from
https://github.com/snowflakedb/snowflake-rest-api-specs).

Every Object Management REST API resource (database, schema, table, view,
procedure, task, stream, warehouse, ...) follows the same shape: a set of
CRUD-ish endpoints keyed by operationId, with path parameters and a JSON
body schema for create/alter. Rather than hand-writing a client class per
resource, we parse each spec once into an `Endpoint` map and let
`ObjectApiClient` (resources/object_api.py) operate generically off it.

To add support for a new resource type: copy its spec YAML from the
snowflake-rest-api-specs repo into /specs and it's picked up automatically
— no new Python code required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

# operationId patterns we care about for the plan/apply engine. Every spec
# also exposes resource-specific actions (:clone, :undrop, :suspend-recluster,
# etc.) which remain available via `ObjectApiClient.call_action`.
_CORE_OPS = {
    "list": {"list"},          # e.g. listTables
    "fetch": {"fetch", "get"},  # e.g. fetchTable
    "create": {"create"},       # e.g. createTable
    "create_or_alter": set(),   # matched via HTTP method PUT instead (see below)
    "delete": {"delete"},       # e.g. deleteTable
}


@dataclass(frozen=True)
class Endpoint:
    method: str          # HTTP method, upper-case
    path_template: str   # e.g. /api/v2/databases/{database}/schemas/{schema}/tables/{name}
    operation_id: str


@dataclass
class ResourceSpec:
    name: str                              # e.g. "table"
    endpoints: dict[str, Endpoint]         # keyed by our normalized op name
    actions: dict[str, Endpoint]           # keyed by the ":action" suffix, e.g. "clone"


class ResourceRegistry:
    def __init__(self) -> None:
        self._resources: dict[str, ResourceSpec] = {}

    @classmethod
    def load_from_dir(cls, specs_dir: str | Path) -> "ResourceRegistry":
        registry = cls()
        specs_dir = Path(specs_dir)
        for spec_file in sorted(specs_dir.glob("*.yaml")):
            if spec_file.stem in ("common", "sqlapi", "result"):
                continue  # not a resource spec
            try:
                registry._load_one(spec_file)
            except Exception as exc:  # noqa: BLE001 - surface which file broke
                raise RuntimeError(f"Failed to parse spec {spec_file.name}: {exc}") from exc
        return registry

    def _load_one(self, spec_file: Path) -> None:
        doc = yaml.safe_load(spec_file.read_text())
        resource_name = spec_file.stem

        endpoints: dict[str, Endpoint] = {}
        actions: dict[str, Endpoint] = {}

        for path_template, methods in doc.get("paths", {}).items():
            for method, op in methods.items():
                if method not in _HTTP_METHODS:
                    continue
                op_id = op.get("operationId", "")
                endpoint = Endpoint(
                    method=method.upper(), path_template=path_template, operation_id=op_id
                )

                if method == "put" and path_template.rstrip("/").endswith("}"):
                    endpoints["create_or_alter"] = endpoint
                    continue
                if ":" in path_template:
                    action_name = path_template.rsplit(":", 1)[-1]
                    if "deprecated" not in op_id.lower():
                        actions[action_name] = endpoint
                    continue

                for normalized, prefixes in _CORE_OPS.items():
                    if normalized == "create_or_alter":
                        continue
                    if any(op_id.lower().startswith(p) for p in prefixes):
                        endpoints[normalized] = endpoint
                        break

        self._resources[resource_name] = ResourceSpec(
            name=resource_name, endpoints=endpoints, actions=actions
        )

    def get(self, resource_name: str) -> ResourceSpec:
        if resource_name not in self._resources:
            available = ", ".join(sorted(self._resources))
            raise KeyError(
                f"No spec loaded for resource '{resource_name}'. "
                f"Available: {available}. Copy its spec YAML into /specs to add it."
            )
        return self._resources[resource_name]

    def available_resources(self) -> list[str]:
        return sorted(self._resources)
