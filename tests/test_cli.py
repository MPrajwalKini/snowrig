"""Tests for the `snowrig` CLI — specifically exit codes and error
presentation, neither of which had any coverage before this file existed.

That gap is exactly how `apply` shipped always returning exit code 0 even
when changes failed or were blocked: nothing exercised the CLI layer at
all, so a CI pipeline checking `$?` after `snowrig apply` had no way to
know anything had gone wrong. These tests lock down:

  - `apply` exits 0 only on a clean run, 1 if anything errored, 2 if
    nothing errored but a destructive change was blocked
  - `plan` exits 1 if it couldn't even compute a diff for an object
  - snowrig's own well-defined exceptions (ManifestError, DependencyError,
    CredentialError) print a clean one-line message instead of a raw
    Python traceback, and still exit non-zero

None of this touches real Snowflake I/O — `snowrig.plan`/`snowrig.apply`
(the module-level functions `cli/main.py` calls) are monkeypatched
directly, the same boundary test_public_api.py already tests below.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import snowrig
from snowrig.cli.main import app
from snowrig.config import CredentialError
from snowrig.manifest.diff import Action, FieldDiff, PlannedChange
from snowrig.manifest.graph import DependencyError
from snowrig.manifest.loader import ManifestError
from snowrig.manifest.schema import ManifestObject, ObjectKey

runner = CliRunner()


def _obj(resource: str = "table", name: str = "DB.PUBLIC.T") -> ManifestObject:
    return ManifestObject(resource=resource, path_params={"name": name}, body={})


def _change(action: Action, *, blocked: str | None = None, key_name: str = "DB.PUBLIC.T") -> PlannedChange:
    return PlannedChange(
        obj=_obj(name=key_name),
        key=ObjectKey(resource="table", qualified_name=key_name),
        action=action,
        diff={},
        blocked=blocked,
    )


# --------------------------------------------------------------------- #
# apply — exit codes
# --------------------------------------------------------------------- #

def test_apply_exits_zero_on_full_success(monkeypatch, tmp_path):
    results = [(_change(Action.CREATE), None)]
    monkeypatch.setattr(snowrig, "apply", lambda *a, **k: results)

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 0
    assert "APPLIED" in result.stdout


def test_apply_exits_zero_when_nothing_to_do(monkeypatch, tmp_path):
    monkeypatch.setattr(snowrig, "apply", lambda *a, **k: [])

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 0
    assert "Nothing to do" in result.stdout


def test_apply_exits_one_when_a_change_errors(monkeypatch, tmp_path):
    results = [(_change(Action.UPDATE), "(400) Bad Request")]
    monkeypatch.setattr(snowrig, "apply", lambda *a, **k: results)

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_apply_exits_two_when_a_change_is_blocked_but_nothing_errors(monkeypatch, tmp_path):
    """Not a crash, not a clean success either — CI should be able to
    tell 'destructive change needs a decision' apart from 'it broke'."""
    results = [(_change(Action.UPDATE, blocked="destructive change requires --allow-destructive"), None)]
    monkeypatch.setattr(snowrig, "apply", lambda *a, **k: results)

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 2
    assert "BLOCKED" in result.stdout


def test_apply_exit_code_prioritizes_error_over_blocked(monkeypatch, tmp_path):
    """If a batch has both a hard failure and a merely-blocked change,
    the failure is the more serious condition and should win the exit
    code (1, not 2)."""
    results = [
        (_change(Action.UPDATE, key_name="DB.PUBLIC.A"), "(400) Bad Request"),
        (_change(Action.UPDATE, blocked="needs --allow-destructive", key_name="DB.PUBLIC.B"), None),
    ]
    monkeypatch.setattr(snowrig, "apply", lambda *a, **k: results)

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 1


# --------------------------------------------------------------------- #
# plan — exit codes
# --------------------------------------------------------------------- #

def test_plan_exits_zero_on_a_normal_diff(monkeypatch, tmp_path):
    monkeypatch.setattr(snowrig, "plan", lambda *a, **k: [_change(Action.NOOP)])

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0


def test_plan_exits_one_when_it_could_not_compute_a_diff_for_an_object(monkeypatch, tmp_path):
    errored = PlannedChange(
        obj=_obj(), key=ObjectKey(resource="table", qualified_name="DB.PUBLIC.T"),
        action=Action.ERROR, diff={}, error="connection reset",
    )
    monkeypatch.setattr(snowrig, "plan", lambda *a, **k: [errored])

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 1


def test_plan_reports_no_objects_found_without_erroring(monkeypatch, tmp_path):
    monkeypatch.setattr(snowrig, "plan", lambda *a, **k: [])

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0
    assert "No .yaml objects found" in result.stdout


# --------------------------------------------------------------------- #
# Known snowrig exceptions print cleanly instead of a raw traceback
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "exc",
    [
        ManifestError("broken.yaml: missing required key 'resource'"),
        DependencyError("Cycle detected among manifest objects: ['table:DB.PUBLIC.A']"),
    ],
)
def test_plan_prints_clean_message_and_exits_one_on_known_errors(monkeypatch, tmp_path, exc):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(snowrig, "plan", _raise)

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 1
    assert str(exc) in result.stdout
    assert "Traceback" not in result.stdout


def test_apply_prints_clean_message_and_exits_one_on_credential_error(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise CredentialError("exactly one of private_key_path/private_key_content must be set")

    monkeypatch.setattr(snowrig, "apply", _raise)

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 1
    assert "private_key_path" in result.stdout
    assert "Traceback" not in result.stdout


def test_exec_prints_clean_message_and_exits_one_on_credential_error(monkeypatch):
    import snowrig.cli.main as cli_module

    def _raise(profile):
        raise CredentialError("profile 'default' not found")

    monkeypatch.setattr(cli_module, "load_profile", _raise)

    result = runner.invoke(app, ["exec", "SELECT 1"])

    assert result.exit_code == 1
    assert "profile 'default' not found" in result.stdout
    assert "Traceback" not in result.stdout