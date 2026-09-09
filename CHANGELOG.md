# Changelog

All notable changes to snowrig are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [semver](https://semver.org/).

## [Unreleased]

Local changes since the `0.3.2` upload — not yet published.

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