"""Adapts snowflake.core's typed, per-resource collections to the generic
fetch/create_or_alter/delete shape snowrig's manifest engine expects — so
the manifest/plan/apply layer doesn't need to know it's talking to
snowflake.core specifically.

Where a resource supports a genuine PUT-style create_or_alter (database,
schema, table, warehouse, ...), we use it directly. Where it doesn't
(procedures/functions, which can't be altered in place), objects are routed
through raw SQL instead — see manifest/diff.py — so this client only ever
handles the resources listed in RESOURCE_MODELS.
"""

from __future__ import annotations

from typing import Any

from snowflake.core import CreateMode
from snowflake.core.exceptions import NotFoundError

from snowrig.resources.core_registry import RESOURCE_MODELS, get_collection


class CoreObjectClient:
    def __init__(self, root: Any):
        self._root = root

    def fetch(self, resource: str, path_params: dict[str, str]) -> dict[str, Any]:
        collection = get_collection(self._root, resource, path_params)
        model = collection[path_params["name"]].fetch()
        return model.to_dict()

    def exists(self, resource: str, path_params: dict[str, str]) -> bool:
        try:
            self.fetch(resource, path_params)
            return True
        except NotFoundError:
            return False

    def create_or_alter(
        self, resource: str, path_params: dict[str, str], body: dict[str, Any]
    ) -> None:
        model_cls = RESOURCE_MODELS[resource]
        collection = get_collection(self._root, resource, path_params)
        model = model_cls(name=path_params["name"], **body)
        item = collection[path_params["name"]]
        if hasattr(item, "create_or_alter"):
            item.create_or_alter(model)
        else:
            collection.create(model, mode=CreateMode.or_replace)

    def delete(self, resource: str, path_params: dict[str, str]) -> None:
        collection = get_collection(self._root, resource, path_params)
        collection[path_params["name"]].drop()
