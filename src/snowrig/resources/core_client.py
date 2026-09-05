"""Adapts snowflake.core's typed, per-resource collections to the generic
fetch/create_or_alter/delete shape snowrig's manifest engine expects — so
the manifest/plan/apply layer doesn't need to know it's talking to
snowflake.core specifically.

Where a resource supports a genuine PUT-style create_or_alter (database,
schema, table, warehouse, ...), we use it directly. Where it doesn't
(procedures/functions, which can't be altered in place), objects are routed
through raw SQL instead — see manifest/diff.py — so this client only ever
handles the resources listed in RESOURCE_MODELS.
"""

from __future__ import annotations

from typing import Any

from snowflake.core import CreateMode
from snowflake.core.exceptions import NotFoundError

from snowrig.resources.core_registry import RESOURCE_MODELS, get_collection

# --- Narrow, explicit field coercion -----------------------------------
#
# snowrig's default is generic passthrough: `ModelClass(name=..., **body)`,
# relying on Pydantic to coerce whatever JSON-primitive values came out of
# YAML into the right field types. This works for the large majority of
# fields, and even works for plain nested-object fields (Pydantic coerces
# a dict into a concrete BaseModel automatically) — but it breaks for two
# categories of field, both handled explicitly below:
#
# 1. Polymorphic/discriminated fields — `Stream.stream_source` is declared
#    as the abstract `StreamSource` base, but the concrete subclasses
#    (StreamSourceTable, StreamSourceView, StreamSourceStage,
#    StreamSourceExternalTable) carry a type discriminator the REST API
#    needs. A plain dict passed to a base-typed field constructs the base
#    class, not the subclass, silently dropping that discriminator — local
#    validation passes, but the API call 400s with no useful message.
#
# 2. Fields with no JSON-primitive equivalent at all — `Task.schedule` is
#    typed as `Cron | timedelta | None`, real Python objects YAML has no
#    native representation for.
#
# See CONTRIBUTING.md for the fuller writeup and the pattern to follow if
# another resource needs the same treatment.


def _coerce_stream_body(body: dict[str, Any]) -> dict[str, Any]:
    stream_source = body.get("stream_source")
    if isinstance(stream_source, dict):
        from snowflake.core.stream import StreamSourceTable

        body = {**body, "stream_source": StreamSourceTable(**stream_source)}
    return body


# datetime.timedelta's own constructor keyword arguments — used to detect
# "this schedule dict means a timedelta" vs "this schedule dict means a
# Cron" purely from its keys, with no separate type tag needed in YAML.
_TIMEDELTA_KWARGS = {"days", "seconds", "microseconds", "milliseconds", "minutes", "hours", "weeks"}


def _coerce_task_body(body: dict[str, Any]) -> dict[str, Any]:
    """Task.schedule is typed as `Cron | timedelta | None` — a real Python
    object with no JSON-primitive equivalent, so the generic passthrough
    can never handle a plain string or dict for it. Accept two explicit
    YAML shapes instead:

        schedule: {minutes: 5}                          -> timedelta(minutes=5)
        schedule: {cron: "*/5 * * * *", timezone: UTC}   -> Cron(expr=..., timezone=...)

    Anything else raises immediately with a clear message rather than
    silently passing a bad value through to snowflake.core.
    """
    schedule = body.get("schedule")
    if not isinstance(schedule, dict):
        return body

    from datetime import timedelta

    from snowflake.core.task import Cron

    keys = set(schedule)
    if "cron" in keys:
        coerced = Cron(expr=schedule["cron"], timezone=schedule.get("timezone", "UTC"))
    elif keys and keys <= _TIMEDELTA_KWARGS:
        coerced = timedelta(**schedule)
    else:
        raise ValueError(
            "task.schedule must be either {cron: '<expr>', timezone: '<tz>'} "
            f"or timedelta keyword args (any of {sorted(_TIMEDELTA_KWARGS)}); "
            f"got keys {sorted(keys)}"
        )
    return {**body, "schedule": coerced}


# Resource name -> function that takes the raw body dict (as loaded from
# YAML) and returns a body dict safe to pass into `ModelClass(**body)`.
# Add an entry here whenever a resource has a field the generic passthrough
# gets wrong (see the note above and CONTRIBUTING.md).
_BODY_COERCERS = {
    "stream": _coerce_stream_body,
    "task": _coerce_task_body,
}


def _extract_api_error_detail(exc: Exception) -> str | None:
    """Pulls the actual response body out of a snowflake.core APIError, since
    str(exc) on these only prints status + reason and drops the body that
    usually contains Snowflake's real error message."""
    http_resp = getattr(exc, "http_resp", None)
    if http_resp is None:
        return None
    body = getattr(http_resp, "data", None)
    if body is None:
        body = getattr(http_resp, "body", None)
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    return str(body).strip() or None


def describe_error(exc: Exception) -> str:
    """Best-effort human-readable message for any exception raised while
    talking to snowflake.core — use this instead of str(exc) when reporting
    failures, so a bare '(400) Reason: Bad Request' doesn't hide the actual
    Snowflake error text."""
    detail = _extract_api_error_detail(exc)
    if detail:
        return f"{exc} — response body: {detail}"
    return str(exc)


class CoreObjectClient:
    def __init__(self, root: Any):
        self._root = root

    def fetch(self, resource: str, path_params: dict[str, str]) -> dict[str, Any]:
        collection = get_collection(self._root, resource, path_params)
        model = collection[path_params["name"]].fetch()
        return model.to_dict()

    def exists(self, resource: str, path_params: dict[str, str]) -> bool:
        try:
            self.fetch(resource, path_params)
            return True
        except NotFoundError:
            return False

    def create_or_alter(
        self, resource: str, path_params: dict[str, str], body: dict[str, Any]
    ) -> None:
        model_cls = RESOURCE_MODELS[resource]
        coercer = _BODY_COERCERS.get(resource)
        if coercer is not None:
            body = coercer(body)
        collection = get_collection(self._root, resource, path_params)
        model = model_cls(name=path_params["name"], **body)
        item = collection[path_params["name"]]
        if hasattr(item, "create_or_alter"):
            item.create_or_alter(model)
        else:
            collection.create(model, mode=CreateMode.or_replace)

    def delete(self, resource: str, path_params: dict[str, str]) -> None:
        collection = get_collection(self._root, resource, path_params)
        collection[path_params["name"]].drop()
