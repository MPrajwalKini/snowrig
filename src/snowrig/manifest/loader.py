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

    return ManifestObject(
        resource=doc["resource"],
        path_params={k: str(v) for k, v in doc["path_params"].items()},
        body=doc.get("body", {}) or {},
        depends_on=list(doc.get("depends_on", []) or []),
        sql=doc.get("sql"),
        source_path=path,
    )