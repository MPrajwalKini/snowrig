"""Walks a manifest directory tree and parses every .yaml file into a
ManifestObject. Directory placement is convention, not enforced — the
resource type and path_params inside the file are the source of truth;
the tree layout is just for human navigation and small diffs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from snowrig.manifest.schema import ManifestObject


class ManifestError(Exception):
    """Raised for malformed manifest files, with the offending path attached."""


def load_manifest_dir(root: str | Path) -> list[ManifestObject]:
    root = Path(root)
    if not root.is_dir():
        raise ManifestError(f"Manifest root '{root}' is not a directory")

    objects: list[ManifestObject] = []
    for yaml_path in sorted(root.rglob("*.yaml")):
        objects.append(_load_one(yaml_path))
    for yml_path in sorted(root.rglob("*.yml")):
        objects.append(_load_one(yml_path))
    return objects


def _load_one(path: Path) -> ManifestObject:
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML — {exc}") from exc

    if "resource" not in doc:
        raise ManifestError(f"{path}: missing required key 'resource'")
    if "path_params" not in doc:
        raise ManifestError(f"{path}: missing required key 'path_params'")
    if not isinstance(doc["path_params"], dict):
        raise ManifestError(
            f"{path}: 'path_params' must be a mapping of key: value pairs "
            f"(e.g. database: DB), got {type(doc['path_params']).__name__}"
        )
    if "name" not in doc["path_params"]:
        raise ManifestError(f"{path}: 'path_params' is missing required key 'name'")
    body = doc.get("body", {}) or {}
    if isinstance(body, dict) and "name" in body:
        # core_client.create_or_alter() constructs the snowflake.core model
        # as `model_cls(name=path_params["name"], **body)` — a `name` key
        # inside `body` collides with that and previously surfaced as a
        # bare `TypeError: got multiple values for keyword argument 'name'`
        # deep inside apply(), pointing nowhere near the actual manifest
        # file. Catch it here instead, at load time, with a message that
        # names the offending file.
        raise ManifestError(
            f"{path}: 'body' must not contain a 'name' key — the object's "
            f"name belongs in 'path_params.name', not 'body.name'"
        )

    return ManifestObject(
        resource=doc["resource"],
        path_params={k: str(v) for k, v in doc["path_params"].items()},
        body=body,
        depends_on=list(doc.get("depends_on", []) or []),
        sql=doc.get("sql"),
        source_path=path,
    )