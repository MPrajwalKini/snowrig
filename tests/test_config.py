from __future__ import annotations

import pytest

from snowrig.config import CredentialError, Profile


def _base(**overrides):
    kwargs = dict(account="acct", user="svc_user")
    kwargs.update(overrides)
    return kwargs


def test_requires_exactly_one_key_source():
    with pytest.raises(CredentialError, match="no private key source"):
        Profile(**_base())


def test_rejects_multiple_key_sources():
    with pytest.raises(CredentialError, match="more than one private key source"):
        Profile(**_base(private_key_path="/tmp/key.pem", private_key="-----BEGIN..."))


def test_resolves_raw_private_key_content():
    profile = Profile(**_base(private_key="-----BEGIN PRIVATE KEY-----\n..."))
    assert profile.resolve_private_key_pem() == b"-----BEGIN PRIVATE KEY-----\n..."


def test_resolves_private_key_from_env_var(monkeypatch):
    monkeypatch.setenv("MY_ORG_SNOWFLAKE_KEY", "pem-content-from-vault-agent")
    profile = Profile(**_base(private_key_env="MY_ORG_SNOWFLAKE_KEY"))
    assert profile.resolve_private_key_pem() == b"pem-content-from-vault-agent"


def test_private_key_env_missing_raises(monkeypatch):
    monkeypatch.delenv("MISSING_KEY_VAR", raising=False)
    profile = Profile(**_base(private_key_env="MISSING_KEY_VAR"))
    with pytest.raises(CredentialError, match="MISSING_KEY_VAR"):
        profile.resolve_private_key_pem()


def test_private_key_path_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.pem"
    profile = Profile(**_base(private_key_path=str(missing)))
    with pytest.raises(CredentialError, match="does not exist"):
        profile.resolve_private_key_pem()


def test_private_key_path_reads_shared_network_style_path(tmp_path):
    # A UNC path or mapped drive behaves the same as any other path here —
    # Path.read_bytes() doesn't distinguish. This just proves the code
    # never assumes "local to this machine".
    key_file = tmp_path / "shared" / "key.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(b"pem-bytes-on-a-share")
    profile = Profile(**_base(private_key_path=str(key_file)))
    assert profile.resolve_private_key_pem() == b"pem-bytes-on-a-share"


def test_rejects_both_passphrase_sources():
    with pytest.raises(CredentialError, match="at most one"):
        Profile(
            **_base(
                private_key="pem",
                private_key_passphrase="hunter2",
                private_key_passphrase_env="PASSPHRASE_VAR",
            )
        )


def test_resolves_passphrase_from_env(monkeypatch):
    monkeypatch.setenv("PASSPHRASE_VAR", "hunter2")
    profile = Profile(
        **_base(private_key="pem", private_key_passphrase_env="PASSPHRASE_VAR")
    )
    assert profile.resolve_passphrase() == "hunter2"


def test_no_passphrase_configured_returns_none():
    profile = Profile(**_base(private_key="pem"))
    assert profile.resolve_passphrase() is None