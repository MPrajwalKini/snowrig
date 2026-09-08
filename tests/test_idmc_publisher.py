from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from snowrig.config import Profile
from snowrig.publishers import idmc


@pytest.fixture
def profile() -> Profile:
    return Profile(
        account="myorg-acct",
        user="SVC_USER",
        private_key="pem-content",
        warehouse="WH",
        role="SYSADMIN",
        database="DB",
        schema="PUBLIC",
    )


def _fake_response(status_code: int, json_data: dict, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text or str(json_data)
    return resp


def test_build_snowflake_connection_body_maps_profile_fields(profile):
    body = idmc.build_snowflake_connection_body(
        profile,
        connection_name="Snowflake_Prod",
        runtime_environment_id="rt-001",
        password="snowpass",
    )
    assert body == {
        "@type": "connection",
        "name": "Snowflake_Prod",
        "type": "Snowflake",
        "runtimeEnvironmentId": "rt-001",
        "account": "myorg-acct",
        "username": "SVC_USER",
        "warehouse": "WH",
        "role": "SYSADMIN",
        "database": "DB",
        "schema": "PUBLIC",
        "password": "snowpass",
    }


def test_build_snowflake_connection_body_extra_attributes_merge_on_top(profile):
    body = idmc.build_snowflake_connection_body(
        profile,
        connection_name="X",
        runtime_environment_id="rt",
        extra_attributes={"privateKey": "PEMDATA", "authenticationType": "KeyPair"},
    )
    assert body["privateKey"] == "PEMDATA"
    assert body["authenticationType"] == "KeyPair"


def test_build_snowflake_connection_body_custom_field_map(profile):
    body = idmc.build_snowflake_connection_body(
        profile,
        connection_name="X",
        runtime_environment_id="rt",
        field_map={"account": "snowflakeAccountName"},
    )
    assert body["snowflakeAccountName"] == "myorg-acct"
    assert "username" not in body  # only fields present in field_map get mapped


@patch("snowrig.publishers.idmc.requests")
def test_login_returns_session(mock_requests):
    mock_requests.post.return_value = _fake_response(
        200, {"serverUrl": "https://na1.example.com", "icSessionId": "sess-123"}
    )
    session = idmc.login("me@org.com", "pw", pod="us")
    assert session.server_url == "https://na1.example.com"
    assert session.session_id == "sess-123"
    called_url = mock_requests.post.call_args.args[0]
    assert called_url == idmc.POD_LOGIN_URLS["us"]


@patch("snowrig.publishers.idmc.requests")
def test_login_failure_raises_idmc_error(mock_requests):
    mock_requests.post.return_value = _fake_response(401, {}, "bad credentials")
    with pytest.raises(idmc.IDMCError, match="bad credentials"):
        idmc.login("me@org.com", "wrong-password")


@patch("snowrig.publishers.idmc.requests")
def test_login_unexpected_response_shape_raises(mock_requests):
    mock_requests.post.return_value = _fake_response(200, {"unexpected": "shape"})
    with pytest.raises(idmc.IDMCError, match="Unexpected IDMC login response"):
        idmc.login("me@org.com", "pw")


@patch("snowrig.publishers.idmc.requests")
def test_get_connector_metadata_sends_query_param(mock_requests):
    session = idmc.IDMCSession(server_url="https://na1.example.com", session_id="sess-123")
    mock_requests.get.return_value = _fake_response(
        200, {"connectorName": "Snowflake Data Cloud", "attributes": ["account", "username"]}
    )
    meta = idmc.get_connector_metadata(session, "Snowflake Data Cloud")
    assert meta["connectorName"] == "Snowflake Data Cloud"
    _, kwargs = mock_requests.get.call_args
    assert kwargs["params"] == {"connectorName": "Snowflake Data Cloud"}
    assert kwargs["headers"]["icSessionId"] == "sess-123"


@patch("snowrig.publishers.idmc.requests")
def test_create_connection_posts_body_and_returns_result(mock_requests):
    session = idmc.IDMCSession(server_url="https://na1.example.com", session_id="sess-123")
    mock_requests.post.return_value = _fake_response(201, {"id": "conn-abc", "name": "Snowflake_Prod"})
    result = idmc.create_connection(session, {"name": "Snowflake_Prod"})
    assert result["id"] == "conn-abc"
    call_kwargs = mock_requests.post.call_args.kwargs
    assert call_kwargs["json"] == {"name": "Snowflake_Prod"}
    assert call_kwargs["headers"]["icSessionId"] == "sess-123"


@patch("snowrig.publishers.idmc.requests")
def test_create_connection_failure_raises(mock_requests):
    session = idmc.IDMCSession(server_url="https://na1.example.com", session_id="sess-123")
    mock_requests.post.return_value = _fake_response(400, {}, "invalid attribute 'foo'")
    with pytest.raises(idmc.IDMCError, match="invalid attribute"):
        idmc.create_connection(session, {"name": "X"})


@patch("snowrig.publishers.idmc.requests")
def test_publish_snowflake_connection_end_to_end(mock_requests, profile):
    def post_side_effect(url, json=None, headers=None, timeout=None):
        if url.endswith("/user/login"):
            return _fake_response(200, {"serverUrl": "https://na1.example.com", "icSessionId": "sess-123"})
        if url.endswith("/api/v2/connection"):
            assert json["account"] == "myorg-acct"
            assert json["password"] == "snowpass"
            return _fake_response(201, {"id": "conn-abc", "name": json["name"]})
        raise AssertionError(f"unexpected URL {url}")

    mock_requests.post.side_effect = post_side_effect

    result = idmc.publish_snowflake_connection(
        profile,
        idmc_username="me@org.com",
        idmc_password="pw",
        connection_name="Snowflake_Prod",
        runtime_environment_id="rt-001",
        snowflake_password="snowpass",
    )
    assert result["id"] == "conn-abc"
    assert result["name"] == "Snowflake_Prod"