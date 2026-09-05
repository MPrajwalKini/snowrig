"""Common authenticator interface.

Both the SQL API and the Object Management REST APIs accept the same
bearer-token style auth (key-pair JWT or OAuth), so every authenticator
in this package just needs to answer one question: "what headers do I
attach to this request right now?" Token refresh/expiry is handled
internally by each implementation.
"""

from __future__ import annotations

from typing import Protocol


class Authenticator(Protocol):
    """Anything that can produce auth headers for a Snowflake REST request."""

    def get_headers(self) -> dict[str, str]:
        """Return headers to merge into an outgoing request (e.g. Authorization)."""
        ...

    def account_url(self) -> str:
        """Return the base URL for this account, e.g. https://<acct>.snowflakecomputing.com"""
        ...
