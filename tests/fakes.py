"""Test doubles standing in for snowflake.core's Root/collections, so
manifest/diff.py and manifest/graph.py can be tested without a live
Snowflake connection. Mirrors the subset of CoreObjectClient's interface
that diff.py actually calls: fetch() and create_or_alter().
"""

from __future__ import annotations

from typing import Any

from snowflake.core.exceptions import NotFoundError


class FakeCoreObjectClient:
    """In-memory stand-in for resources.core_client.CoreObjectClient.

    `live` is keyed by (resource, name) -> body dict, pre-seeded to
    represent "what Snowflake currently has". fetch() raises NotFoundError
    for anything not seeded, matching the real client's behavior when
    snowflake.core can't find the object.
    """

    def __init__(self, live: dict[tuple[str, str], dict[str, Any]] | None = None):
        self.live: dict[tuple[str, str], dict[str, Any]] = live or {}
        self.applied: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def fetch(self, resource: str, path_params: dict[str, str]) -> dict[str, Any]:
        key = (resource, path_params["name"])
        if key not in self.live:
            raise NotFoundError("not found")
        return self.live[key]

    def create_or_alter(self, resource: str, path_params: dict[str, str], body: dict[str, Any]) -> None:
        self.applied.append((resource, dict(path_params), dict(body)))
        key = (resource, path_params["name"])
        self.live[key] = {**self.live.get(key, {}), **body}

    def exists(self, resource: str, path_params: dict[str, str]) -> bool:
        return (resource, path_params["name"]) in self.live