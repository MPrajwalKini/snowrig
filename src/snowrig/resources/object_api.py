"""Generic client for the Snowflake Object Management REST APIs.

Instead of one hand-written class per resource type, this operates off the
`ResourceRegistry` (parsed from Snowflake's own OpenAPI specs). Every
resource — database, schema, table, view, procedure, task, stream,
warehouse, and anything else you vendor a spec for — goes through the same
create_or_alter / fetch / list / delete / call_action methods.

This is what makes `create_or_alter` interesting: it's a genuine PUT-based
create-or-alter at the API level, so the manifest/plan/apply engine doesn't
need to maintain its own state file to know CREATE vs ALTER — Snowflake
already knows.
"""

from __future__ import annotations

from typing import Any

from snowrig.client.http import SnowflakeHttpClient
from snowrig.resources.registry import ResourceRegistry


class ObjectApiClient:
    def __init__(self, http: SnowflakeHttpClient, registry: ResourceRegistry):
        self._http = http
        self._registry = registry

    async def list(self, resource: str, *, path_params: dict[str, str]) -> list[dict]:
        spec = self._registry.get(resource)
        endpoint = spec.endpoints.get("list")
        if not endpoint:
            raise NotImplementedError(f"{resource} has no list endpoint in its spec")
        path = _fill_path(endpoint.path_template, path_params)
        resp = await self._http.request(endpoint.method, path)
        return resp.json()

    async def fetch(self, resource: str, *, path_params: dict[str, str]) -> dict:
        spec = self._registry.get(resource)
        endpoint = spec.endpoints.get("fetch")
        if not endpoint:
            raise NotImplementedError(f"{resource} has no fetch endpoint in its spec")
        path = _fill_path(endpoint.path_template, path_params)
        resp = await self._http.request(endpoint.method, path)
        return resp.json()

    async def exists(self, resource: str, *, path_params: dict[str, str]) -> bool:
        try:
            await self.fetch(resource, path_params=path_params)
            return True
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                return False
            raise

    async def create_or_alter(
        self,
        resource: str,
        *,
        path_params: dict[str, str],
        body: dict[str, Any],
        query_params: dict[str, str] | None = None,
    ) -> dict | None:
        """Idempotent create-or-alter — the core primitive for `snowrig apply`.

        Not every resource has a native PUT create-or-alter endpoint (notably
        procedures and functions, which are identified by name+signature and
        can't be altered in place). For those, we fall back to POST create
        with createMode=orReplace, which is Snowflake's equivalent.
        """
        spec = self._registry.get(resource)
        endpoint = spec.endpoints.get("create_or_alter")
        if not endpoint:
            create_endpoint = spec.endpoints.get("create")
            if not create_endpoint:
                raise NotImplementedError(
                    f"{resource} has neither a create-or-alter nor a create endpoint"
                )
            merged_query = {"createMode": "orReplace", **(query_params or {})}
            return await self.create(
                resource, path_params=path_params, body=body, query_params=merged_query
            )
        path = _fill_path(endpoint.path_template, path_params)
        effective_body = _inject_name(path_params, body)
        resp = await self._http.request(
            endpoint.method, path, json_body=effective_body, params=query_params
        )
        return resp.json() if resp.content else None

    async def create(
        self,
        resource: str,
        *,
        path_params: dict[str, str],
        body: dict[str, Any],
        query_params: dict[str, str] | None = None,
    ) -> dict | None:
        spec = self._registry.get(resource)
        endpoint = spec.endpoints.get("create")
        if not endpoint:
            raise NotImplementedError(f"{resource} has no create endpoint in its spec")
        path = _fill_path(endpoint.path_template, path_params)
        # POST create endpoints never include {name} in the path (only
        # PUT/GET/DELETE do) — Snowflake expects it in the body instead.
        effective_body = _inject_name(path_params, body)
        resp = await self._http.request(
            endpoint.method, path, json_body=effective_body, params=query_params
        )
        return resp.json() if resp.content else None

    async def delete(
        self, resource: str, *, path_params: dict[str, str], query_params: dict | None = None
    ) -> None:
        spec = self._registry.get(resource)
        endpoint = spec.endpoints.get("delete")
        if not endpoint:
            raise NotImplementedError(f"{resource} has no delete endpoint in its spec")
        path = _fill_path(endpoint.path_template, path_params)
        await self._http.request(endpoint.method, path, params=query_params)

    async def call_action(
        self,
        resource: str,
        action: str,
        *,
        path_params: dict[str, str],
        body: dict[str, Any] | None = None,
        query_params: dict | None = None,
    ) -> dict | None:
        """Invoke a resource-specific action, e.g. clone/undrop/suspend-recluster."""
        spec = self._registry.get(resource)
        endpoint = spec.actions.get(action)
        if not endpoint:
            raise NotImplementedError(
                f"{resource} has no '{action}' action. Available: {sorted(spec.actions)}"
            )
        path = _fill_path(endpoint.path_template.split(":")[0], path_params) + f":{action}"
        resp = await self._http.request(
            endpoint.method, path, json_body=body, params=query_params
        )
        return resp.json() if resp.content else None


def _inject_name(path_params: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    """Snowflake's object schemas require `name` in the body on every write
    operation (create AND create-or-alter), even when it's already present
    in the URL path — confirmed against the Database/Table/etc. OpenAPI
    schemas, which all mark `name` as required."""
    if "name" in path_params and "name" not in body:
        return {"name": path_params["name"], **body}
    return body


def _fill_path(template: str, params: dict[str, str]) -> str:
    path = template
    for key, value in params.items():
        path = path.replace(f"{{{key}}}", value)
    if "{" in path:
        raise ValueError(f"Unfilled path parameter(s) in '{path}' — provided: {params}")
    return path
