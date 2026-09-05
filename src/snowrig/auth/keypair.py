"""Key-pair (JWT) authentication, Snowflake's recommended method for
service/non-interactive REST API access. Used identically by the SQL API
and every Object Management REST API.

Reference: https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/authentication
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

# JWTs are short-lived; Snowflake caps them at 1 hour. We refresh a bit early.
_TOKEN_LIFETIME_SECONDS = 55 * 60
_REFRESH_SKEW_SECONDS = 60


def _normalize_account(account: str) -> str:
    """Best-effort normalization of an account identifier for JWT subject/issuer use.

    Snowflake's JWT scheme historically wants the account *locator* segment only
    (no region/cloud suffix) for some account identifier formats. If your account
    uses the newer "orgname-accountname" identifier, pass it through as-is; if you
    hit auth errors with a legacy "<locator>.<region>.<cloud>" identifier, strip
    everything after the first "." before passing it in, or adjust here.
    """
    return account.upper()


def _public_key_fingerprint(private_key: RSAPrivateKey) -> str:
    public_key_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(public_key_der).digest()
    return "SHA256:" + base64.b64encode(digest).decode("utf-8")


@dataclass
class KeyPairAuthenticator:
    """Generates and caches short-lived JWTs signed with an RSA private key.

    Parameters
    ----------
    account:
        Your Snowflake account identifier (used to build the account URL and the
        JWT issuer/subject).
    user:
        The Snowflake user this key pair is registered against
        (`ALTER USER ... SET RSA_PUBLIC_KEY = ...`).
    private_key_path:
        Path to a PEM-encoded PKCS#8 private key.
    private_key_passphrase:
        Optional passphrase if the key is encrypted.
    account_url_override:
        Optional full base URL if your account doesn't follow the default
        `https://<account>.snowflakecomputing.com` shape (e.g. private link).
    """

    account: str
    user: str
    private_key_path: str
    private_key_passphrase: str | None = None
    account_url_override: str | None = None

    def __post_init__(self) -> None:
        self._account_norm = _normalize_account(self.account)
        self._user_norm = self.user.upper()
        self._private_key = self._load_private_key()
        self._fingerprint = _public_key_fingerprint(self._private_key)
        self._cached_token: str | None = None
        self._cached_exp: float = 0.0

    def _load_private_key(self) -> RSAPrivateKey:
        key_bytes = Path(self.private_key_path).read_bytes()
        password = (
            self.private_key_passphrase.encode("utf-8")
            if self.private_key_passphrase
            else None
        )
        key = serialization.load_pem_private_key(key_bytes, password=password)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("Snowflake key-pair auth requires an RSA private key.")
        return key

    def _mint_token(self) -> str:
        now = int(time.time())
        qualified_user = f"{self._account_norm}.{self._user_norm}"
        payload = {
            "iss": f"{qualified_user}.{self._fingerprint}",
            "sub": qualified_user,
            "iat": now,
            "exp": now + _TOKEN_LIFETIME_SECONDS,
        }
        token = jwt.encode(payload, self._private_key, algorithm="RS256")
        self._cached_token = token
        self._cached_exp = now + _TOKEN_LIFETIME_SECONDS
        return token

    def _current_token(self) -> str:
        if self._cached_token is None or time.time() > (
            self._cached_exp - _REFRESH_SKEW_SECONDS
        ):
            return self._mint_token()
        return self._cached_token

    def get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._current_token()}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        }

    def account_url(self) -> str:
        if self.account_url_override:
            return self.account_url_override.rstrip("/")
        return f"https://{self.account.lower()}.snowflakecomputing.com"
