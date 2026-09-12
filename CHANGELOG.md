# Changelog

All notable changes to snowrig are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [semver](https://semver.org/).

## [Unreleased]

0.4.0 scope — reliability/safety hardening pass, no new resource types.
See the audit notes in this entry for full P0/P1/P2 rationale.

### Fixed (P0 — a blocked destructive change silently halted unrelated changes)
- `apply_plan()`'s default `stop_on_error=True` treated a *blocked*
  destructive change the same as a genuine failure: it would `break` out
  of the loop, meaning every other object later in the plan — however
  unrelated, however safe — was never even attempted. Worse, those
  skipped objects didn't appear in the returned results at all, so
  nothing in the CLI's output indicated they'd been skipped; a person
  would see "Applied/attempted 1 change(s)" and have no way to know 19
  other legitimate changes silently never ran. This directly contradicted
  the documented behavior ("the gate only affects changes that would
  drop something") and had never been caught because every existing
  destructive-change test used a plan with exactly one object — the bug
  only shows up with two or more. Fixed in `manifest/diff.py`: a blocked
  change no longer respects `stop_on_error` — it's an intentional,
  expected skip, not a failure, and every other object in the batch is
  still attempted normally. `stop_on_error`'s original purpose (halting
  on a genuine exception or an `Action.ERROR` from a failed fetch) is
  unchanged. New regression test in `test_diff.py` using a two-table
  plan, which fails under the old code and passes under the fix.

### Fixed (P0/P1 — CLI never signaled failure via exit code)
- `snowrig apply` always exited 0, even when a change failed outright or
  a destructive change was blocked — a CI pipeline checking `$?` after
  `snowrig apply` had no way to know anything had gone wrong short of
  parsing stdout. Root cause: **the CLI had zero test coverage** (no
  `test_cli.py` existed at all), so nothing exercised this path. Fixed
  in `cli/main.py`: `apply` now exits 1 if any change errored, 2 if
  nothing errored but a destructive change was blocked (needs
  `--allow-destructive` or a manifest fix — not a crash, but not a
  clean success either), 0 otherwise. `plan` now exits 1 if it couldn't
  even compute a diff for one or more objects (`Action.ERROR`). New
  `tests/test_cli.py` (11 tests) covers this and is the first CLI
  coverage of any kind.
- Separately, none of the CLI commands caught snowrig's own
  well-defined exceptions (`ManifestError`, `DependencyError`,
  `CredentialError`) — a malformed manifest or bad profile config
  surfaced as a raw Python traceback instead of the exception's already-
  clear message. `plan`/`apply`/`exec`/`transaction`/`fetch` now catch
  these and print a clean `Error: ...` line before exiting 1.

### Fixed (P1 — destructive-change safety gap)
- The destructive-change gate only ever caught column *removal*. A
  column *type* change that shrinks representable range — narrowing a
  `VARCHAR`/`CHAR` length, reducing a `NUMBER`/`DECIMAL`'s precision or
  scale, or switching type family entirely (e.g. `VARCHAR` -> `NUMBER`)
  — sailed through as a plain, unprotected `UPDATE`, which is exactly
  the kind of change `--allow-destructive` exists to gate. Confirmed
  against a real account (smoke test Round 4b): Snowflake actually
  *refuses* a `VARCHAR` length reduction outright rather than silently
  truncating it (`CREATE OR ALTER TABLE` errors with "reducing the
  byte-length of a varchar is not supported") — so the practical risk
  ranges from a hard `apply()` failure to real data loss depending on
  the types involved, and `plan()` surfacing it up front either way is
  the point. Fixed in `manifest/diff.py`: added
  `_is_lossy_datatype_narrowing()` and a new `FieldDiff.narrowed_items`
  (alongside the existing `removed_items`) that drives `is_destructive`
  and shows up in `destructive_summary()` / `snowrig plan`'s output the
  same way a dropped column does. Widening changes (`VARCHAR(100)` ->
  `VARCHAR(150)`, `NUMBER(10,2)` -> `NUMBER(20,2)`) are correctly left
  alone. 8 new tests in `test_diff.py`, plus live coverage in
  `smoke_test.py` Round 4b.

### Fixed (P1 — dynamic-table create/alter always 400s)
- `dynamic-table`'s `target_lag` is typed by `snowflake.core` as a real
  `UserDefinedLag` object, not a plain dict — the same class of problem
  `_coerce_stream_body` already solved for `Stream.stream_source`, just
  missed for this resource type when the 5 new types were wired up in
  0.3.7. A manifest's `target_lag: {seconds: N}` passed local validation
  (the field is loosely typed enough to accept a dict) but failed
  server-side with an opaque, bodyless `(400) Bad Request` — every
  `dynamic-table` `apply()` failed unconditionally. Caught by the smoke
  test's new dynamic-table round-trip coverage. Fixed in
  `resources/core_client.py`: added `_coerce_dynamic_table_body()`,
  registered in `_BODY_COERCERS` alongside stream/task. The stale
  comment in `examples/manifests/.../dynamic-table.customers_summary.yaml`
  claiming no coercion was needed here has been corrected. 3 new tests
  in `test_core_client.py`.

### Fixed (P1 — HTTP server reliability)
- `/v1/query` never evicted a broken cached connection on failure —
  only `/v1/test-connection` did. Once a cached connection actually died
  server-side (dropped network connection, expired session) mid-query,
  every subsequent `/v1/query` call for that profile kept retrying the
  same dead connection instead of reconnecting. Fixed in `api/server.py`:
  on a query failure, if the connection itself is now closed, it's
  evicted from the cache so the next request reconnects — a plain SQL
  error (bad syntax, missing table) leaves the connection open and is
  *not* treated as a reason to reconnect.

### Fixed (P2 — security hardening)
- Bearer-token comparison in `api/server.py`'s `_authed()` used `!=`,
  which short-circuits on the first mismatched byte — a timing side
  channel that leaks how many leading characters of a guessed token were
  correct. Switched to `hmac.compare_digest()`.

### Fixed (P2 — performance)
- `manifest/graph.py`'s `topological_order()` re-sorted the "ready" queue
  at every step using `nodes.index(...)` inside the sort key — an O(n)
  lookup evaluated per comparison, making the whole tie-break
  effectively O(n² log n) on top of Kahn's algorithm for no behavioral
  difference. Replaced with a precomputed `{key: index}` map so each
  lookup is O(1); output ordering is byte-for-byte identical (existing
  determinism tests pass unmodified).

### Fixed (P2 — error clarity)
- A manifest `body:` containing a `name` key (e.g. accidentally
  duplicating `path_params.name`) previously surfaced as a bare
  `TypeError: got multiple values for keyword argument 'name'` deep
  inside `apply()`'s `snowflake.core` model construction, with no
  indication of which manifest file caused it. `manifest/loader.py` now
  catches this — and a `path_params` missing the required `name` key —
  at load time, with a `ManifestError` naming the offending file. 2 new
  tests in `test_loader.py`.

### Fixed (P2 — dead code removed)
- `manifest/schema.py`'s `ManifestObject.fetch_path_params()` — meant to
  build the `nameWithArgs`-suffixed path for overloadable resources
  (procedure/function) — was never called anywhere. Procedures/functions
  are always SQL-backed (`compute_plan()` short-circuits on `obj.sql`
  before ever calling `client.fetch()`), so the method was dead since
  the day it was written and, worse, implied overloaded-resource
  fetching was wired up when it isn't. Removed; `_arg_signature_suffix()`
  (which *is* used, by `key()`, for dependency-graph disambiguation)
  is unaffected.

### Notes
- Audited but deliberately left unchanged this pass: whole-object
  deletion when a manifest file is removed (still a no-op — snowrig
  never drops an object just because its YAML disappeared; this is a
  documented scope choice, not a bug, since reversing it would require
  either full state tracking or listing every live object per resource
  type on every plan), connection-layer auth (unchanged — key-pair only,
  OAuth is a real 0.5.x candidate, not a hardening item), and the apply
  engine's stop-on-error/continue-on-error/dry-run semantics (reviewed,
  already behave predictably and don't promise rollback Snowflake can't
  provide).

## [0.3.7] — TestPyPI

### Added
- `snowrig.Session` / `snowrig.session()` — holds one connection open
  across multiple `plan()`/`apply()`/`query()` calls instead of opening a
  fresh one per call. `plan()`/`apply()` are now thin one-call wrappers
  around a `Session` opened and closed for that single call; their
  observable behavior is unchanged (verified: all existing connection-
  lifecycle tests pass without modification).
- 5 new resource types: `dynamic-table`, `event-table`, `pipe`,
  `sequence`, `stage` — bringing total coverage to 12. These were already
  anticipated in `manifest/schema.py`'s `SCHEMA_SCOPED_RESOURCES` but
  never wired up in `core_registry.py` until now. Example manifests added
  for all 5 under `examples/manifests/`.
- `tests/test_session.py` (11 tests) and `tests/test_core_registry.py`
  (26 tests) — first direct coverage of either.

### Fixed
- `apply()` of a table update that adds a new column anywhere but the end
  of the manifest's `columns:` list (e.g. a new column declared in the
  same position as one being dropped) failed with Snowflake's
  `unsupported feature 'create or alter table column add before end of
  column list'` — `CREATE OR ALTER TABLE` only accepts new columns at
  the very end of the column list, but `body["columns"]` was sent
  straight from the manifest in whatever order it was declared. Caught
  by the smoke test's Round 4 (destructive-change gate). Fixed in
  `resources/core_client.py`: `create_or_alter` now fetches the table's
  live column order first and reorders just the outgoing `columns` list
  so genuinely new columns land last, while every existing column keeps
  its live relative position — this only affects what's sent over the
  wire, not the manifest's own declared order or how `plan()` renders
  the diff. New tests in `test_core_client.py`.

### Notes
- Deliberately still excludes grants/roles/users from resource coverage
  — see `core_registry.py`'s module docstring for why.

## [0.3.2] — TestPyPI

### Added
- `SqlRunner.run_query()` — returns cursor-shaped `(columns, rows,
  rowcount)` rather than `run()`'s list-of-dicts, for callers building
  their own tabular response. `api/server.py`'s `/v1/test-connection` and
  `/v1/query` handlers now delegate to it instead of duplicating cursor
  open/execute/close logic inline.
- `/v1/query` accepts an optional `params` array for parameterized
  queries, bound natively by the driver rather than requiring callers to
  string-format values into the SQL themselves.
- `tests/test_sql.py` — first direct coverage of `SqlRunner` (previously
  untested).

### Fixed
- `SqlRunner.run()` briefly regressed to a `NameError` (`params` referenced
  before it existed as a parameter) during the `run_query()` refactor —
  caught via the test suite and live `snowrig serve` testing before being
  published.

## [0.3.1] — TestPyPI

### Added
- `snowrig serve` — an HTTP API (`fastapi`/`uvicorn`, install via the
  `api` extra) so platforms without a Python runtime or Snowflake driver
  can run SQL through a named profile without ever holding the private
  key themselves. Routes: `/v1/health`, `/v1/profiles`, `/v1/test-connection`,
  `/v1/query`. Bearer-token auth on every route except `/v1/health`.
  Connections are cached per profile and transparently reopened if closed.
- Private key resolution decoupled from "must be a local file": profiles
  now support `private_key_path` (a file — local, mapped drive, or UNC
  network share), `private_key_env` (the *name* of an environment
  variable holding the raw PEM content — the profile itself never
  contains a secret), or `private_key` (raw PEM content directly, for
  programmatic use). Same pattern for `private_key_passphrase` /
  `private_key_passphrase_env`. Exactly one key source must be set;
  validated at profile-construction time via `CredentialError`.
- Test coverage added for the diff engine, dependency graph, manifest
  loader, field coercion, credential resolution, the public library API's
  connection-lifecycle handling, and the `serve` HTTP API (auth
  enforcement, `/v1/profiles` never leaking key material, query
  success/error paths, connection caching).
- CI (GitHub Actions) running the full test suite on push/PR across
  Python 3.10–3.12.

### Fixed
- `manifest/diff.py`: a column present live but removed from the
  manifest was previously undetected as destructive — `plan` reported it
  as a generic `UPDATE` with no indication that `apply` would drop the
  column and its data. Now flagged explicitly (`⚠ DESTRUCTIVE`), and
  `apply` refuses to execute any destructive change unless
  `--allow-destructive` is passed.

### Removed
- ~35% dead code: a hand-rolled REST/JWT client (`resources/object_api.py`,
  `resources/registry.py`, `client/`, `auth/`) and the vendored OpenAPI
  spec dump (`specs/`) it was built from, orphaned since the migration to
  `snowflake.core` — nothing in the live codebase imported any of it.

## [0.3.0] — Yanked

Published to TestPyPI, then deleted after discovering the installed
package pointed at a nonexistent module (`snowrig.connection_bkp`)
introduced during local development, not present in `0.3.1`'s source.
Version number retired; do not attempt to reuse it.

## [0.2.0] — TestPyPI

Initial published version. `plan`/`apply`/`exec`/`transaction`/`fetch`
CLI commands, dependency-ordered manifest deployment via `snowflake.core`'s
`create_or_alter`, public `plan()`/`apply()` library API with `connection=`
support for reusing an existing connection, key-pair auth via the
official Snowflake Connector for Python.