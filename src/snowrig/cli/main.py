from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table
from snowflake.core import Root

from snowrig.config import DEFAULT_CONFIG_PATH, load_profile
from snowrig.connection import connect
from snowrig.manifest.diff import Action, apply_plan, compute_plan
from snowrig.manifest.graph import build_apply_order
from snowrig.manifest.loader import load_manifest_dir
from snowrig.resources.core_client import CoreObjectClient
from snowrig.sql import SqlRunner

app = typer.Typer(help="snowrig — free, always-available Snowflake automation on top of Snowflake's own SDKs.")
console = Console()


@app.command()
def init(profile: str = "default") -> None:
    """Interactively create a connection profile at ~/.snowrig/config.yaml."""
    account = typer.prompt("Account identifier (e.g. myorg-myaccount)")
    user = typer.prompt("Username")
    key_path = typer.prompt("Path to PEM private key")
    key_passphrase = typer.prompt(
        "Private key passphrase (leave blank if unencrypted)", default="", hide_input=True
    )
    warehouse = typer.prompt("Default warehouse", default="")
    role = typer.prompt("Default role", default="")

    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {}
    if DEFAULT_CONFIG_PATH.exists():
        doc = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    doc.setdefault("profiles", {})[profile] = {
        "account": account,
        "user": user,
        "private_key_path": key_path,
        "private_key_passphrase": key_passphrase or None,
        "warehouse": warehouse or None,
        "role": role or None,
    }
    DEFAULT_CONFIG_PATH.write_text(yaml.safe_dump(doc, sort_keys=False))
    console.print(f"[green]Wrote profile '{profile}' to {DEFAULT_CONFIG_PATH}[/green]")


@app.command()
def exec(
    statement: str = typer.Argument(..., help="SQL statement to run"),
    profile: str = "default",
) -> None:
    """Run a single SQL statement and print the results."""
    prof = load_profile(profile)
    conn = connect(prof)
    try:
        rows = SqlRunner(conn).run(
            statement, warehouse=prof.warehouse, role=prof.role,
            database=prof.database, schema=prof.schema,
        )
        if not rows:
            console.print("[dim]No rows returned.[/dim]")
            return
        table = Table(*rows[0].keys())
        for row in rows:
            table.add_row(*[str(v) for v in row.values()])
        console.print(table)
    finally:
        conn.close()


@app.command()
def transaction(
    statements: list[str] = typer.Argument(
        ..., help="SQL statements to run atomically, each as a separate quoted argument"
    ),
    profile: str = "default",
) -> None:
    """Run multiple statements as a single transaction (all commit together, or none do)."""
    prof = load_profile(profile)
    conn = connect(prof)
    try:
        SqlRunner(conn).run_transaction(
            statements, warehouse=prof.warehouse, role=prof.role,
            database=prof.database, schema=prof.schema,
        )
        console.print(f"[green]Transaction committed[/green] — {len(statements)} statement(s)")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Transaction rolled back:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def fetch(
    resource: str = typer.Argument(..., help="Resource type, e.g. table, view, warehouse"),
    path: str = typer.Argument(..., help='Path params as key=value pairs, e.g. database=DB schema=PUBLIC name=MY_TABLE'),
    profile: str = "default",
) -> None:
    """Fetch a single object's current definition."""
    prof = load_profile(profile)
    conn = connect(prof)
    try:
        root = Root(conn)
        client = CoreObjectClient(root)
        path_params = dict(p.split("=", 1) for p in path.split())
        result = client.fetch(resource, path_params)
        console.print_json(json.dumps(result, default=str))
    finally:
        conn.close()


def _action_style(action: Action) -> str:
    return {
        Action.CREATE: "[green]create[/green]",
        Action.UPDATE: "[yellow]update[/yellow]",
        Action.NOOP: "[dim]no-op[/dim]",
        Action.ERROR: "[red]error[/red]",
    }[action]


def _load_and_order(manifest_dir: str):
    objects = load_manifest_dir(manifest_dir)
    if not objects:
        console.print(f"[yellow]No .yaml objects found under {manifest_dir}[/yellow]")
    return build_apply_order(objects)


@app.command()
def plan(
    manifest_dir: str = typer.Argument(..., help="Path to the manifest directory"),
    profile: str = "default",
) -> None:
    """Diff a manifest against live Snowflake state without changing anything."""
    ordered = _load_and_order(manifest_dir)
    prof = load_profile(profile)
    conn = connect(prof)
    try:
        client = CoreObjectClient(Root(conn))
        plan_result = compute_plan(ordered, client)

        table = Table("Action", "Resource", "Object", "Changed fields")
        for change in plan_result:
            fields = ", ".join(change.diff.keys()) if change.diff else (change.error or "")
            if change.is_destructive:
                fields = f"[red]\u26a0 DESTRUCTIVE[/red] {fields} \u2014 {change.destructive_summary()}"
            table.add_row(
                _action_style(change.action), change.key.resource, change.key.qualified_name, fields
            )
        console.print(table)
    finally:
        conn.close()


@app.command()
def apply(
    manifest_dir: str = typer.Argument(..., help="Path to the manifest directory"),
    profile: str = "default",
    dry_run: bool = typer.Option(False, help="Show what would happen without executing anything"),
    continue_on_error: bool = typer.Option(
        False, help="Keep applying remaining objects after a failure instead of stopping"
    ),
    allow_destructive: bool = typer.Option(
        False,
        help=(
            "Allow changes that drop columns or other manifest-declared items. "
            "Without this, destructive changes are skipped and reported as BLOCKED."
        ),
    ),
) -> None:
    """Apply a manifest to Snowflake, in dependency order."""
    ordered = _load_and_order(manifest_dir)
    prof = load_profile(profile)
    conn = connect(prof)
    try:
        client = CoreObjectClient(Root(conn))
        sql_runner = SqlRunner(conn)
        plan_result = compute_plan(ordered, client)

        actionable = [c for c in plan_result if c.action in (Action.CREATE, Action.UPDATE)]
        if not actionable:
            console.print("[dim]Nothing to do — live state already matches the manifest.[/dim]")
            return

        console.print(f"Applying {len(actionable)} change(s){' (dry run)' if dry_run else ''}...")
        results = apply_plan(
            plan_result, client, sql_runner=sql_runner,
            warehouse=prof.warehouse, role=prof.role,
            dry_run=dry_run, stop_on_error=not continue_on_error,
            allow_destructive=allow_destructive,
        )

        for change, error in results:
            label = f"{change.key.resource}:{change.key.qualified_name}"
            if change.blocked:
                console.print(f"  [yellow]BLOCKED[/yellow]  {label} \u2014 {change.blocked}")
            elif error:
                console.print(f"  [red]FAILED[/red]  {label} \u2014 {error}")
            elif dry_run:
                console.print(f"  [dim]WOULD APPLY[/dim]  {label}")
            else:
                console.print(f"  [green]APPLIED[/green]  {label}")
    finally:
        conn.close()


@app.command()
def resources() -> None:
    """List resource types currently supported by the typed object client."""
    from snowrig.resources.core_registry import RESOURCE_MODELS

    for name in sorted(RESOURCE_MODELS):
        console.print(f"  {name}")
    console.print("  [dim]procedure, function — via raw SQL (`sql:` in the manifest), not this list[/dim]")


if __name__ == "__main__":
    app()