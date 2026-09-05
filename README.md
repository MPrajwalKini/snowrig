# snowrig

A free, always-available, Python-native way to declare and deploy Snowflake
objects from YAML — without adopting Terraform/HCL/Go just for this, and
without hand-writing imperative `snowflake.core` scripts for every object.

## Honest positioning

This space already has strong, mature tools, and snowrig isn't trying to
out-build them:

- **[Terraform Provider for Snowflake](https://github.com/snowflakedb/terraform-provider-snowflake)**
  — the mature, officially-backed choice for serious infra-as-code. If you
  already use Terraform, use this instead.
- **[snowflake.core](https://docs.snowflake.com/en/developer-guide/snowflake-python-api/snowflake-python-overview)**
  — Snowflake's own official Python SDK for object management. snowrig is
  *built on top of this*, not competing with it — see below.
- **[schemachange](https://github.com/Snowflake-Labs/schemachange)** —
  mature, widely-used versioned SQL migration tool, if what you want is
  Flyway-style change history rather than declarative diffing.

snowrig's actual niche: a lightweight **declarative YAML manifest + plan/apply
diff engine**, in pure Python, for teams who want that workflow without a
second toolchain. That's it — not "everything possible," a specific gap.

## Architecture

snowrig is built entirely on Snowflake's own official SDKs:

- **[`snowflake.core`](https://docs.snowflake.com/en/developer-guide/snowflake-python-api/snowflake-python-overview)**
  for typed object management (database, schema, table, view, warehouse,
  stream, task). `snowflake.core` already implements the REST correctness
  work — Snowflake's own engineers maintain it — so snowrig's job is just
  the manifest/diff/ordering layer on top, not re-solving REST edge cases.
- **The [Snowflake Connector for Python](https://docs.snowflake.com/en/user-guide/python-connector)**
  for raw SQL execution, transactions, and anything with a real SQL body
  (procedures, functions) — `CREATE OR REPLACE` via the connector is more
  robust than forcing procedures through a typed object schema.

An earlier version of this project hand-rolled a REST client and JWT signer
against Snowflake's SQL API and Object Management REST APIs directly. That
version is gone — every edge case it hit (procedure name+signature lookup,
missing PUT endpoints, `name` required in the body) turned out to already
be correctly handled by `snowflake.core`, so re-solving them by hand added
risk without adding value.

## Why create-or-alter still matters here

Even on top of `snowflake.core`, the core idea holds: most resources
(database, schema, table, warehouse, ...) expose a genuine
`resource.create_or_alter(model)` method — Snowflake itself knows whether
to create or alter, so snowrig's `apply` doesn't need to maintain its own
state file to figure that out. `plan`/`apply` just `fetch()` the live
object, diff it against your YAML, and `create_or_alter()` the difference.

Procedures and functions don't support in-place alteration (they're
identified by name + argument signature, and can be overloaded), so those
are declared with a raw `sql:` body instead and applied via
`CREATE OR REPLACE` through the connector — see the example manifest.

## Setup

```bash
pip install -e .
snowrig init                 # writes ~/.snowrig/config.yaml
snowrig exec "SELECT CURRENT_VERSION()"
snowrig plan examples/manifests
snowrig apply examples/manifests
```

Or via environment variables (CI-friendly, no config file):

```bash
export SNOWRIG_ACCOUNT=myorg-myaccount
export SNOWRIG_USER=SVC_USER
export SNOWRIG_PRIVATE_KEY_PATH=/path/to/key.pem
snowrig exec "SELECT 1"
```

## Manifest format

One YAML file per object, directory tree mirrors database → schema:

```
manifests/
  account/
    warehouse.compute_wh.yaml     # no database/schema
  MY_DB/
    database.yaml
    PUBLIC/
      schema.yaml
      table.customers.yaml
      procedure.my_proc.yaml       # uses `sql:` instead of `body:`
```

Typed objects:

```yaml
resource: table
path_params:
  database: MY_DB
  schema: PUBLIC
  name: CUSTOMERS
body:
  columns:
    - name: ID
      datatype: NUMBER(38,0)
```

SQL-defined objects (procedures, functions):

```yaml
resource: procedure
path_params:
  database: MY_DB
  schema: PUBLIC
  name: my_proc
body:
  arguments:               # only used to identify the object for display
    - name: table_name
      datatype: VARCHAR
sql: |
  create or replace procedure my_proc(table_name varchar)
  returns varchar
  language javascript
  as
  $$
    ...
  $$;
```

## Status

Working: key-pair auth via the official connector, `snowrig exec`,
`snowrig transaction`, `snowrig fetch`, `snowrig plan`, `snowrig apply`,
dependency-ordered manifest deployment.

Not yet built: broader resource coverage (`snowflake.core` supports far
more than the 6 types wired up in `resources/core_registry.py` — adding
one is a few lines, see that file), grants/roles, OAuth as an auth option,
tests, CI, PyPI packaging.

## Layout

```
src/snowrig/
├── connection.py     # key-pair auth via the official connector
├── sql.py            # SQL execution + transactions via the connector
├── resources/
│   ├── core_registry.py  # resource name -> snowflake.core model + collection path
│   └── core_client.py    # generic fetch/create_or_alter/delete adapter
├── manifest/          # schema, loader, dependency graph, plan/apply diff engine
├── cli/               # typer entrypoint
└── config.py           # connection profiles
```
