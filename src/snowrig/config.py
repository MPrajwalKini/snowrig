"""Connection profile loading.

Profiles live in ~/.snowrig/config.yaml (or a path passed via --config),
keyed by profile name, so you can juggle multiple accounts the way the
Snowflake CLI's connections.toml does. Environment variables override
file values so this drops cleanly into CI.

Private key resolution is deliberately decoupled from "a file must exist
on this machine". Exactly one of these must be set per profile:

  private_key_path      - a filesystem path (local, a mapped drive, or a
                           UNC network share — Path.read_bytes() doesn't
                           care which). The straightforward case, but the
                           key still has to physically sit somewhere the
                           caller's filesystem can reach.

  private_key_env       - the *name* of an environment variable that
                           holds the raw PEM key content. The profile
                           itself (this YAML file, or a shared/checked-in
                           config) never contains a secret or a path to
                           one — just the name of wherever the local
                           environment already injects it (CI secrets, a
                           Vault agent, an OS keychain export into the
                           shell, direnv, a launch.json "env" block,
                           etc). How that variable gets set is not
                           snowrig's concern; it just reads whatever's
                           there at connect() time. This is the option to
                           reach for when you don't want the key on disk
                           anywhere near the machine running VS Code.

  private_key           - the raw PEM content directly, already in hand.
                           Meant for programmatic use — building a
                           Profile in code after fetching a secret from
                           wherever your own tooling talks to (AWS
                           Secrets Manager, Vault, Azure Key Vault, ...)
                           — rather than something you'd write into a
                           YAML file.

private_key_passphrase / private_key_passphrase_env follow the same
pattern for the key's passphrase, if it's encrypted.

Deliberately NOT included: direct SDKs for any particular secrets
manager. If you already fetch a secret from one, resolve it yourself and
either set private_key_env to point at where you put it, or build a
Profile in code with private_key set directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".snowrig" / "config.yaml"


class CredentialError(ValueError):
    """Raised when a profile's private-key source is missing, ambiguous, or unresolvable."""


@dataclass
class Profile:
    account: str
    user: str
    private_key_path: str | None = None
    private_key_env: str | None = None
    private_key: str | None = None
    private_key_passphrase: str | None = None
    private_key_passphrase_env: str | None = None
    warehouse: str | None = None
    role: str | None = None
    database: str | None = None
    schema: str | None = None

    def __post_init__(self) -> None:
        key_sources = [self.private_key_path, self.private_key_env, self.private_key]
        provided = [s for s in key_sources if s]
        if len(provided) == 0:
            raise CredentialError(
                "Profile has no private key source — set exactly one of "
                "private_key_path, private_key_env, or private_key."
            )
        if len(provided) > 1:
            raise CredentialError(
                "Profile has more than one private key source set — pick exactly "
                "one of private_key_path, private_key_env, or private_key."
            )
        if self.private_key_passphrase and self.private_key_passphrase_env:
            raise CredentialError(
                "Set at most one of private_key_passphrase / private_key_passphrase_env, not both."
            )

    def resolve_private_key_pem(self) -> bytes:
        """Returns the raw PEM key content as bytes, from whichever source
        this profile is configured to use."""
        if self.private_key:
            return (
                self.private_key.encode("utf-8")
                if isinstance(self.private_key, str)
                else self.private_key
            )
        if self.private_key_env:
            value = os.environ.get(self.private_key_env)
            if not value:
                raise CredentialError(
                    f"Profile references private_key_env='{self.private_key_env}' "
                    f"but that environment variable is not set (or empty)."
                )
            return value.encode("utf-8")
        # private_key_path — __post_init__ already guarantees exactly one is set.
        path = Path(self.private_key_path)
        if not path.exists():
            raise CredentialError(f"private_key_path={path} does not exist.")
        return path.read_bytes()

    def resolve_passphrase(self) -> str | None:
        if self.private_key_passphrase:
            return self.private_key_passphrase
        if self.private_key_passphrase_env:
            value = os.environ.get(self.private_key_passphrase_env)
            if not value:
                raise CredentialError(
                    f"Profile references private_key_passphrase_env="
                    f"'{self.private_key_passphrase_env}' but that environment "
                    f"variable is not set (or empty)."
                )
            return value
        return None


def load_profile(name: str = "default", config_path: Path | None = None) -> Profile:
    # Env vars win outright — convenient for CI, no file needed at all.
    # SNOWRIG_PRIVATE_KEY (raw content) and SNOWRIG_PRIVATE_KEY_ENV (a var
    # name to look up) are both accepted alongside the original
    # SNOWRIG_PRIVATE_KEY_PATH — Profile.__post_init__ enforces exactly one.
    if os.environ.get("SNOWRIG_ACCOUNT"):
        return Profile(
            account=os.environ["SNOWRIG_ACCOUNT"],
            user=os.environ["SNOWRIG_USER"],
            private_key_path=os.environ.get("SNOWRIG_PRIVATE_KEY_PATH"),
            private_key_env=os.environ.get("SNOWRIG_PRIVATE_KEY_ENV"),
            private_key=os.environ.get("SNOWRIG_PRIVATE_KEY"),
            private_key_passphrase=os.environ.get("SNOWRIG_PRIVATE_KEY_PASSPHRASE"),
            private_key_passphrase_env=os.environ.get("SNOWRIG_PRIVATE_KEY_PASSPHRASE_ENV"),
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


def load_profiles_from_file(config_path: Path | None = None) -> dict[str, Profile]:
    """Loads every profile from the YAML config file, keyed by name.

    Unlike load_profile(), this always reads the file and never takes the
    SNOWRIG_* env-var shortcut — there's no single "the" profile to
    shortcut to when a caller needs to pick among several by name at
    request time. That's the case for `snowrig serve`: one server process,
    many profiles, the caller names one per request.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `snowrig init` to create one, or pass --config."
        )
    doc = yaml.safe_load(path.read_text()) or {}
    profiles = doc.get("profiles", {})
    return {name: Profile(**fields) for name, fields in profiles.items()}