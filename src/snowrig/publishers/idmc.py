"""Push a snowrig Profile out to Informatica IDMC as a native Snowflake
connection, using IDMC's own REST API.

This solves a different problem than `snowrig serve` (see snowrig.api):
that exposes Snowflake *out* over HTTP for another platform to call into.
This instead calls *IDMC's* REST API to create a connection object inside
IDMC itself, so IDMC ends up with a normal, native Snowflake connection it
can use in its own mappings and tasks — snowrig isn't involved after
creation.

Verified against Informatica's public REST API reference:
  Login:               POST https://dm-<pod>.informaticacloud.com/ma/api/v2/user/login
                        body: {"username", "password"}
                        -> {"serverUrl": ..., "icSessionId": ...}
  Connector metadata:   GET  <serverUrl>/api/v2/connector/metadata?connectorName=<name>
                        Returns the attribute names a given connector type
                        actually expects. Informatica's own docs are
                        explicit that these vary by connector type (and can
                        vary by org/connector version) — this endpoint is
                        the source of truth, not this module's field map.
  Create connection:    POST <serverUrl>/api/v2/connection
                        body: {"@type": "connection", "name", "type",
                               "runtimeEnvironmentId", <connector fields>}

NOT verified against a live IDMC org: the exact attribute names your
org's Snowflake connector expects for anything beyond the basic
account/username/warehouse/role/database/schema fields — in particular,
key-pair or OAuth auth field names are connector-version-specific.
Call get_connector_metadata() first and pass field_map / extra_attributes
to match what it returns before relying on this in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from snowrig.config import Profile

# Add PODs as you need them — see Informatica's Product Availability
# Matrix (PAM) for the full current list. You can also pass a full login
# URL directly as `pod` to bypass this map entirely.
POD_LOGIN_URLS = {
    "us": "https://dm-us.informaticacloud.com/ma/api/v2/user/login",
    "eu": "https://dm-eu.informaticacloud.com/ma/api/v2/user/login",
    "ap": "https://dm-ap.informaticacloud.com/ma/api/v2/user/login",
}


class IDMCError(RuntimeError):
    """Raised on IDMC login, metadata-lookup, or connection-creation failures."""


@dataclass
class IDMCSession:
    server_url: str
    session_id: str

    def headers(self) -> dict[str, str]:
        return {
            "icSessionId": self.session_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def login(username: str, password: str, pod: str = "us", timeout: float = 30) -> IDMCSession:
    """Logs in to IDMC and returns a session for subsequent v2 calls.

    Resolving `password` (e.g. from an env var, matching the
    private_key_env pattern in snowrig.config) is the caller's job — this
    function just uses whatever string it's given.
    """
    url = POD_LOGIN_URLS.get(pod, pod)
    resp = requests.post(url, json={"username": username, "password": password}, timeout=timeout)
    if resp.status_code != 200:
        raise IDMCError(f"IDMC login failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    try:
        return IDMCSession(server_url=data["serverUrl"], session_id=data["icSessionId"])
    except KeyError as e:
        raise IDMCError(f"Unexpected IDMC login response shape, missing {e}: {data}") from e


def get_connector_metadata(session: IDMCSession, connector_name: str, timeout: float = 30) -> dict[str, Any]:
    """Fetches the attribute schema IDMC expects for a given connector type.

    Run this before create_connection for any connector type/org you
    haven't used before — this is the authoritative source for field
    names, not SNOWFLAKE_BASIC_AUTH_FIELD_MAP below.
    """
    resp = requests.get(
        f"{session.server_url}/api/v2/connector/metadata",
        params={"connectorName": connector_name},
        headers=session.headers(),
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise IDMCError(f"Fetching connector metadata failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


# Best-effort defaults for a basic-auth Snowflake connector, based on
# Informatica's published how-to for creating a Snowflake connection
# (UserName / Password / Account / Warehouse / Role). Confirm with
# get_connector_metadata() first, especially if your org's connector uses
# key-pair or OAuth auth — those field names aren't captured here.
SNOWFLAKE_BASIC_AUTH_FIELD_MAP: dict[str, str] = {
    "account": "account",
    "user": "username",
    "warehouse": "warehouse",
    "role": "role",
    "database": "database",
    "schema": "schema",
}


def build_snowflake_connection_body(
    profile: Profile,
    *,
    connection_name: str,
    runtime_environment_id: str,
    connector_type: str = "Snowflake",
    password: str | None = None,
    field_map: dict[str, str] | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds the POST body for creating an IDMC connection from a snowrig
    Profile.

    snowrig profiles authenticate with a key pair; most IDMC Snowflake
    connector variants historically expect a username/password pair
    instead, so:
      - fields this module is confident about (account, user, warehouse,
        role, database, schema) are mapped via field_map (defaults to
        SNOWFLAKE_BASIC_AUTH_FIELD_MAP),
      - `password` is taken explicitly rather than reusing the profile's
        private key as a password — they are not interchangeable,
      - anything connector-specific (a private-key field, OAuth
        client id/secret, a different auth-type attribute) goes through
        extra_attributes, merged on top of the mapped fields.
    """
    field_map = field_map or SNOWFLAKE_BASIC_AUTH_FIELD_MAP
    profile_values = {
        "account": profile.account,
        "user": profile.user,
        "warehouse": profile.warehouse,
        "role": profile.role,
        "database": profile.database,
        "schema": profile.schema,
    }

    body: dict[str, Any] = {
        "@type": "connection",
        "name": connection_name,
        "type": connector_type,
        "runtimeEnvironmentId": runtime_environment_id,
    }
    for snowrig_field, idmc_field in field_map.items():
        value = profile_values.get(snowrig_field)
        if value is not None:
            body[idmc_field] = value
    if password is not None:
        body["password"] = password
    if extra_attributes:
        body.update(extra_attributes)
    return body


def create_connection(session: IDMCSession, body: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    """POSTs a connection body (see build_snowflake_connection_body) to
    IDMC and returns the created connection object, including its id."""
    resp = requests.post(
        f"{session.server_url}/api/v2/connection",
        json=body,
        headers=session.headers(),
        timeout=timeout,
    )
    if resp.status_code not in (200, 201):
        raise IDMCError(f"Creating IDMC connection failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def publish_snowflake_connection(
    profile: Profile,
    *,
    idmc_username: str,
    idmc_password: str,
    connection_name: str,
    runtime_environment_id: str,
    snowflake_password: str | None = None,
    pod: str = "us",
    connector_type: str = "Snowflake",
    field_map: dict[str, str] | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-call convenience: log in to IDMC, build the connection body from
    a snowrig Profile, create it, return the created connection.

    Example — pushing a key-pair-auth profile through, once you've
    confirmed your org's connector field name for the private key via
    get_connector_metadata():

        import os
        import snowrig
        from snowrig.publishers.idmc import publish_snowflake_connection

        profile = snowrig.config.load_profile("prod")
        result = publish_snowflake_connection(
            profile,
            idmc_username="me@myorg.com",
            idmc_password=os.environ["IDMC_PASSWORD"],
            connection_name="Snowflake_Prod",
            runtime_environment_id="0012ABC000000099",
            extra_attributes={
                "privateKey": profile.resolve_private_key_pem().decode(),
                "authenticationType": "KeyPair",
            },
        )
        print(result["id"])
    """
    session = login(idmc_username, idmc_password, pod=pod)
    body = build_snowflake_connection_body(
        profile,
        connection_name=connection_name,
        runtime_environment_id=runtime_environment_id,
        connector_type=connector_type,
        password=snowflake_password,
        field_map=field_map,
        extra_attributes=extra_attributes,
    )
    return create_connection(session, body)