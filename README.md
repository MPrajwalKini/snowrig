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

`private_key_path` (above) is the simplest option, but not the only one —
see **Keeping the private key off the machine running this** below if you
don't want the key sitting in a file next to your config, e.g. on a
laptop running VS Code.

## Keeping the private key off the machine running this

A profile's private key doesn't have to be a local file. Exactly one of
these goes in a profile (in `~/.snowrig/config.yaml`, or the matching
`SNOWRIG_*` env var):

| Profile field | Env var | What it is |
|---|---|---|
| `private_key_path` | `SNOWRIG_PRIVATE_KEY_PATH` | A filesystem path — local, a mapped drive, or a UNC network share. The key still has to physically sit somewhere the caller's filesystem can reach. |
| `private_key_env` | `SNOWRIG_PRIVATE_KEY_ENV` | The *name* of an environment variable holding the raw PEM content. The config file itself never contains a secret — just the name of wherever your environment already injects one (CI secrets, a Vault agent, an OS keychain export, a `launch.json` "env" block, `direnv`, ...). How that variable gets set isn't snowrig's concern; it's read at connect time. |
| `private_key` | `SNOWRIG_PRIVATE_KEY` | The raw PEM content directly. Meant for programmatic use — build a `Profile` in code after fetching a secret from wherever your own tooling already talks to (AWS Secrets Manager, Vault, Azure Key Vault, ...), rather than writing it into YAML. |

`private_key_passphrase` / `private_key_passphrase_env` follow the same
pattern if the key is encrypted.

Deliberately not included: SDKs for any specific secrets manager. If you
already fetch a secret from one, either point `private_key_env` at where
you put it, or build a `Profile` in code with `private_key` set directly
— snowrig doesn't need to know which vault you used.

## Plan / apply, and destructive changes

`snowrig plan` diffs every manifest object against live Snowflake state
without changing anything. If a change would drop something Snowflake
currently has that the manifest no longer declares — a removed column,
for example — the plan marks it explicitly:

```
Action   Resource   Object              Changed fields
update   table      MY_DB.PUBLIC.CUSTOMERS   ⚠ DESTRUCTIVE columns — columns: EMAIL will be dropped
```

`snowrig apply` refuses to execute any destructive change by default —
it's reported as `BLOCKED`, not silently skipped or silently applied. To
actually let a destructive change through, pass `--allow-destructive`:

```bash
snowrig apply examples/manifests --allow-destructive
```

Non-destructive changes (new columns, new objects, scalar field updates)
always apply normally; the gate only affects changes that would drop
something.

## Using snowrig as a library

`plan()`/`apply()` are also a public, importable API — the CLI is a thin
wrapper around the same two functions, nothing it does isn't available
this way:

```python
import snowrig

result = snowrig.plan("manifests/", profile="prod")
for change in result:
    if change.is_destructive:
        print("would drop:", change.destructive_summary())

snowrig.apply("manifests/", profile="prod")
```

Pass `connection=` to reuse a connection you already have open (e.g.
inside an Airflow task or a service with its own connection pool) instead
of having snowrig open and close its own:

```python
snowrig.apply("manifests/", connection=my_existing_connection)
```

## Running queries from other platforms — `snowrig serve`

Everything above assumes something that can run Python and hold a private
key. `snowrig serve` covers the case where that's not true — a platform
that can make an HTTP request but has no Snowflake driver, no Python
runtime, and shouldn't be handed key material at all (a low-code tool, a
script on a machine you don't fully trust, a teammate who just needs to
run one query without setting up a connector).

```bash
pip install "snowrig[api]"
export SNOWRIG_API_TOKEN=$(openssl rand -hex 32)   # long random value; every request must present it
snowrig serve --config ~/.snowrig/config.yaml
```

The server holds every profile's private key itself. Callers only ever
send a profile *name* and SQL, and get rows back — the key material never
leaves the machine running `snowrig serve`.

```
GET  /v1/health           # no auth — for load balancer / uptime checks
GET  /v1/profiles         # lists configured profile names + account/warehouse/role
                           # (never returns key material or passphrases)
POST /v1/test-connection  # {"profile": "default"} -> {"ok": true|false, "error"?: "..."}
POST /v1/query             # {"profile": "default", "sql": "SELECT ...", "params"?: [...]}
                           # -> {"columns", "rows", "rowcount"}
```

Every route except `/v1/health` requires `Authorization: Bearer <SNOWRIG_API_TOKEN>`.

```bash
curl -H "Authorization: Bearer $SNOWRIG_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"profile": "default", "sql": "SELECT CURRENT_VERSION()"}' \
     http://127.0.0.1:8420/v1/query
```

`sql` accepts an optional `params` array alongside it for parameterized
queries — the connector binds these natively rather than you
string-formatting a value into `sql` yourself, which matters most exactly
where this endpoint is most likely to be used: building a query from
input a caller sent you.

```bash
curl -H "Authorization: Bearer $SNOWRIG_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"profile": "default", "sql": "SELECT * FROM customers WHERE id = %s", "params": [123]}' \
     http://127.0.0.1:8420/v1/query
```

`params` is entirely optional — omit it for queries with no placeholders.

**What this is not.** `/v1/query` runs whatever SQL the caller sends,
with whatever privileges that profile's role has — DDL and DML included.
There's no query allowlisting, no per-caller row-level auth, and no rate
limiting; the token gates *access to the server*, not *what an
authenticated caller can do with it*. This is deliberate scope — building
a general-purpose SQL gateway/firewall is a different, much bigger
project, and not one that makes sense to bolt onto snowrig. If you need
that, put `snowrig serve` behind your own reverse proxy/gateway rather
than exposing `--host 0.0.0.0` directly, and scope each profile's
Snowflake role down to only what that profile's callers should be able
to do — the server enforces authentication, Snowflake's own role/grant
system is what should enforce authorization.

Also unlike `snowrig plan`/`apply`, there's no destructive-change gate
here — `/v1/query` is raw SQL execution, not the manifest diff engine, so
`--allow-destructive` doesn't apply. A query that drops a table just
drops the table.

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

Working: key-pair auth via the official connector (with the private key
resolvable from a file, an env var indirection, or raw content — see
above, not just a local path), `snowrig exec`, `snowrig transaction`,
`snowrig fetch`, `snowrig plan`, `snowrig apply` (with a destructive-
change gate — see above), `snowrig serve` (an HTTP API for running SQL
from platforms without a Snowflake driver or key material of their own —
see above), dependency-ordered manifest deployment, explicit coercion for
the handful of non-JSON-primitive fields that need it (`Task.schedule`,
`Stream.stream_source` — see CONTRIBUTING.md), a public `plan()`/`apply()`
library API for embedding in your own scripts/pipelines, and a pytest
suite covering the diff engine, dependency graph, manifest loader, field
coercion, credential resolution, the public API's connection-lifecycle
handling, and the `serve` API's auth/routing.

Not yet built: broader resource coverage (`snowflake.core` supports far
more than the 6 types wired up in `resources/core_registry.py` — adding
one is a few lines, see that file), grants/roles, OAuth as an auth
option, PyPI packaging (published to TestPyPI; not yet to real PyPI),
query allowlisting/rate limiting on `snowrig serve` (deliberately out of
scope for now — see the API section above).

## Layout

```
src/snowrig/
├── __init__.py       # public plan()/apply() library API
├── connection.py     # key-pair auth via the official connector
├── sql.py            # SQL execution + transactions via the connector
├── resources/
│   ├── core_registry.py  # resource name -> snowflake.core model + collection path
│   └── core_client.py    # generic fetch/create_or_alter/delete adapter
├── manifest/          # schema, loader, dependency graph, plan/apply diff engine
├── api/                # FastAPI app behind `snowrig serve` (needs the `api` extra)
├── cli/               # typer entrypoint (thin wrapper over the library API)
└── config.py           # connection profiles + credential resolution
tests/                  # pytest suite — fakes/doubles, no live Snowflake needed
```