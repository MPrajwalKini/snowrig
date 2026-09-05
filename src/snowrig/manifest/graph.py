"""Orders manifest objects so `apply` never touches something before its
prerequisites exist: database before schema, schema before table/view/proc,
plus any explicit `depends_on` edges for things the hierarchy can't infer
(e.g. a task that reads from a stream in a different object).
"""

from __future__ import annotations

from dataclasses import dataclass

from snowrig.manifest.schema import ManifestObject, ObjectKey


class DependencyError(Exception):
    """Raised for cycles or references to objects that don't exist in the manifest."""


@dataclass
class PlanNode:
    obj: ManifestObject
    key: ObjectKey
    depends_on: set[ObjectKey]


def _parse_dependency_string(s: str) -> ObjectKey:
    if ":" not in s:
        raise DependencyError(
            f"Invalid depends_on entry '{s}' — expected 'resource:qualified.name', "
            f"e.g. 'stream:MY_DB.PUBLIC.MY_STREAM'"
        )
    resource, qualified_name = s.split(":", 1)
    return ObjectKey(resource=resource, qualified_name=qualified_name)


def build_plan_nodes(objects: list[ManifestObject]) -> list[PlanNode]:
    nodes: list[PlanNode] = []
    known_keys: set[ObjectKey] = {obj.key() for obj in objects}

    for obj in objects:
        key = obj.key()
        deps: set[ObjectKey] = set()

        implicit = obj.implicit_dependency()
        if implicit is not None:
            deps.add(implicit)

        for raw in obj.depends_on:
            dep_key = _parse_dependency_string(raw)
            deps.add(dep_key)

        # Only enforce ordering against deps that are actually part of this
        # manifest — a dependency on something managed outside snowrig (e.g.
        # a pre-existing shared database) is fine, just not orderable here.
        deps = {d for d in deps if d in known_keys}

        nodes.append(PlanNode(obj=obj, key=key, depends_on=deps))

    return nodes


def topological_order(nodes: list[PlanNode]) -> list[PlanNode]:
    """Kahn's algorithm. Raises DependencyError with the cycle's members on failure."""
    by_key = {n.key: n for n in nodes}
    in_degree = {n.key: 0 for n in nodes}
    dependents: dict[ObjectKey, list[ObjectKey]] = {n.key: [] for n in nodes}

    for n in nodes:
        for dep in n.depends_on:
            in_degree[n.key] += 1
            dependents[dep].append(n.key)

    # Stable order: process ready nodes in the order they appear in `nodes`.
    ready = [n.key for n in nodes if in_degree[n.key] == 0]
    ordered: list[PlanNode] = []

    while ready:
        ready.sort(key=lambda k: nodes.index(by_key[k]))
        current = ready.pop(0)
        ordered.append(by_key[current])
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(nodes):
        remaining = [str(k) for k in by_key if k not in {o.key for o in ordered}]
        raise DependencyError(
            f"Cycle detected among manifest objects (or unresolved cross-refs): {remaining}"
        )

    return ordered


def build_apply_order(objects: list[ManifestObject]) -> list[ManifestObject]:
    nodes = build_plan_nodes(objects)
    ordered = topological_order(nodes)
    return [n.obj for n in ordered]
