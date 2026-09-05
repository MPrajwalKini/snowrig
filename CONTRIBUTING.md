# Contributing to snowrig

Thanks for considering it — this is a small, hand-maintained project, so
most contributions fall into one of a few well-defined shapes below.

## Adding a new resource type

snowrig deliberately does *not* codegen its resource list — `core_registry.py`
is a small manual lookup table from a snowrig resource name to a
`snowflake.core` model class + how to navigate `Root` to its collection.

To add one:

1. Import the model class from `snowflake.core` in
   `src/snowrig/resources/core_registry.py`.
2. Add it to `RESOURCE_MODELS`.
3. Add a branch in `get_collection()` for how to reach its collection from
   `Root` (most schema-scoped resources are
   `root.databases[db].schemas[schema].<plural>`).
4. If the resource is schema-scoped, add its name to
   `SCHEMA_SCOPED_RESOURCES` in `manifest/schema.py` so dependency ordering
   treats it correctly.
5. Add an example manifest under `examples/manifests/` exercising it.

That's usually the whole change — the manifest/plan/apply/diff engine is
generic over `RESOURCE_MODELS` and doesn't need to know about the new type.

## Field coercion for non-primitive fields

`CoreObjectClient.create_or_alter()` constructs objects as
`ModelClass(name=..., **body)`, where `body` is whatever your YAML's `body:`
key contains, loaded straight from YAML/JSON primitives (str, int, float,
bool, list, dict).

This works cleanly for the large majority of fields, which really are
JSON-primitive. It also works, with no extra code, for fields typed as a
*concrete* nested Pydantic `BaseModel` — Pydantic v2 automatically coerces
a plain nested dict into the right object as part of normal validation.

It breaks in two different ways, both handled explicitly via
`core_client.py`'s `_BODY_COERCERS` table rather than left as manifest
workarounds:

**1. Fields with no dict/primitive equivalent at all** — `Task.schedule`
is typed as `Cron | timedelta | None`, real Python objects YAML has no
native representation for. `_coerce_task_body()` accepts two explicit
YAML shapes and converts them before constructing the model:

```yaml
schedule: {minutes: 5}                        # -> timedelta(minutes=5)
schedule: {cron: "*/5 * * * *", timezone: UTC} # -> Cron(expr=..., timezone=...)
```

See `task.process_customer_changes.yaml` for a working example. Anything
else in the `schedule:` dict raises immediately with a clear message
rather than silently passing a bad value through.

**2. Fields typed as a polymorphic/discriminated base class** —
`Stream.stream_source` is declared as the abstract `StreamSource` base,
but the real concrete subclasses (`StreamSourceTable`, `StreamSourceView`,
`StreamSourceStage`, `StreamSourceExternalTable`) carry a type
discriminator the REST API needs to know which kind of source this is.
A plain dict passed to a field typed as the *base* class gets coerced into
a `StreamSource` instance, not the subclass — which passes local Pydantic
validation (so you won't see an error until the API call itself), but the
serialized request is missing the discriminator and Snowflake's API
rejects it with a content-free 400. `_coerce_stream_body()` explicitly
constructs `StreamSourceTable` before handing the body to
`ModelClass(**body)` — see `stream.customers_stream.yaml`.

Also worth knowing: some fields that *look* settable are actually
read-only / server-computed (e.g. `Stream.table_name`, which is display
metadata, not input — the actual source is `stream_source`). Passing a
value there won't raise a helpful error pointing you at the right field;
you'll just get a validation failure on the field you *did* set. Worth
double-checking a resource's docs for "Read-only" annotations before
assuming a field name is the one you want.

**If another resource needs the same treatment:** follow the pattern in
`_BODY_COERCERS` — a small, explicit, per-resource/per-field function that
takes the raw body dict and returns one safe to pass into
`ModelClass(**body)`. Keep it narrow rather than trying to build a generic
YAML→arbitrary-Python-object coercer; the goal is covering the handful of
fields that actually need it, not solving deserialization in general.

## Other known gaps (see README "Status")

- Grants/roles support
- OAuth as an auth option
- Tests
- CI
- PyPI packaging

## General guidelines

- Keep `core_registry.py` a flat lookup table — no cleverness, no codegen.
- Prefer routing anything with a real SQL body (procedures, functions)
  through the SQL connector rather than trying to force it through a typed
  `snowflake.core` model — see the README's "Architecture" section for why.
- Add or update an example manifest for any new resource type or behavior.
- Keep changes small and focused; this project is intentionally not trying
  to cover "everything possible."
