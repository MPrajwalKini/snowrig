"""Thin shared HTTP layer used by both the SQL API client and the Object
Management API client. Handles auth header injection, JSON encode/decode,
and retry on transient failures (429/5xx).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from snowrig.auth.base import Authenticator

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BASE_BACKOFF_SECONDS = 0.5


class SnowflakeHttpError(Exception):
    def __init__(self, status_code: int, body: Any, request_desc: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"{request_desc} failed with HTTP {status_code}: {body}")


class SnowflakeHttpClient:
    """Wraps httpx.AsyncClient with Snowflake auth + retry semantics."""

    def __init__(self, authenticator: Authenticator, timeout: float = 60.0):
        self._auth = authenticator
        self._client = httpx.AsyncClient(
            base_url=self._auth.account_url(), timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SnowflakeHttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._auth.get_headers(),
            **(extra_headers or {}),
        }

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            response = await self._client.request(
                method, path, json=json_body, params=params, headers=headers
            )
            if response.status_code not in _RETRYABLE_STATUS:
                if response.status_code >= 400:
                    raise SnowflakeHttpError(
                        response.status_code, _safe_body(response), f"{method} {path}"
                    )
                return response
            last_exc = SnowflakeHttpError(
                response.status_code, _safe_body(response), f"{method} {path}"
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2**attempt))
        assert last_exc is not None
        raise last_exc


def _safe_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
