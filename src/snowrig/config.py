"""Connection profile loading.

Profiles live in ~/.snowrig/config.yaml (or a path passed via --config),
keyed by profile name, so you can juggle multiple accounts the way the
Snowflake CLI's connections.toml does. Environment variables override
file values so this drops cleanly into CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".snowrig" / "config.yaml"


@dataclass
class Profile:
    account: str
    user: str
    private_key_path: str
    private_key_passphrase: str | None = None
    warehouse: str | None = None
    role: str | None = None
    database: str | None = None
    schema: str | None = None


def load_profile(name: str = "default", config_path: Path | None = None) -> Profile:
    # Env vars win outright — convenient for CI, no file needed at all.
    if os.environ.get("SNOWRIG_ACCOUNT"):
        return Profile(
            account=os.environ["SNOWRIG_ACCOUNT"],
            user=os.environ["SNOWRIG_USER"],
            private_key_path=os.environ["SNOWRIG_PRIVATE_KEY_PATH"],
            private_key_passphrase=os.environ.get("SNOWRIG_PRIVATE_KEY_PASSPHRASE"),
            warehouse=os.environ.get("SNOWRIG_WAREHOUSE"),
            role=os.environ.get("SNOWRIG_ROLE"),
            database=os.environ.get("SNOWRIG_DATABASE"),
            schema=os.environ.get("SNOWRIG_SCHEMA"),
        )

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path} and no SNOWRIG_* env vars set. "
            f"Run `snowrig init` to create one."
        )
    doc = yaml.safe_load(path.read_text())
    profiles = doc.get("profiles", {})
    if name not in profiles:
        raise KeyError(f"No profile '{name}' in {path}. Available: {sorted(profiles)}")
    return Profile(**profiles[name])
