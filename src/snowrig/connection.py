"""Builds a snowflake.connector.Connection using key-pair authentication.

This replaces snowrig's original hand-rolled JWT signing. The official
connector already implements JWT construction, refresh, and every account-
identifier edge case correctly — we just hand it the private key in the DER
format it expects and let it do the rest.
"""

from __future__ import annotations

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from snowrig.config import Profile


def _pem_to_der(pem_bytes: bytes, passphrase: str | None) -> bytes:
    password = passphrase.encode("utf-8") if passphrase else None
    private_key = serialization.load_pem_private_key(pem_bytes, password=password)
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect(profile: Profile) -> snowflake.connector.SnowflakeConnection:
    pem_bytes = profile.resolve_private_key_pem()
    passphrase = profile.resolve_passphrase()
    kwargs: dict = {
        "account": profile.account,
        "user": profile.user,
        "private_key": _pem_to_der(pem_bytes, passphrase),
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