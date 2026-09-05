"""Builds a snowflake.connector.Connection using key-pair authentication.

This replaces snowrig's original hand-rolled JWT signing. The official
connector already implements JWT construction, refresh, and every account-
identifier edge case correctly — we just hand it the private key in the DER
format it expects and let it do the rest.
"""

from __future__ import annotations

from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from snowrig.config import Profile


def _load_private_key_der(path: str, passphrase: str | None) -> bytes:
    key_bytes = Path(path).read_bytes()
    password = passphrase.encode("utf-8") if passphrase else None
    private_key = serialization.load_pem_private_key(key_bytes, password=password)
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect(profile: Profile) -> snowflake.connector.SnowflakeConnection:
    kwargs: dict = {
        "account": profile.account,
        "user": profile.user,
        "private_key": _load_private_key_der(
            profile.private_key_path, profile.private_key_passphrase
        ),
    }
    if profile.warehouse:
        kwargs["warehouse"] = profile.warehouse
    if profile.role:
        kwargs["role"] = profile.role
    if profile.database:
        kwargs["database"] = profile.database
    if profile.schema:
        kwargs["schema"] = profile.schema
    return snowflake.connector.connect(**kwargs)
