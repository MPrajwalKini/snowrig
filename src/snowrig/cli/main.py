from __future__ import annotations

import json
import os
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table
from snowflake.core import Root

import snowrig
from snowrig import Action
from snowrig.config import DEFAULT_CONFIG_PATH, load_profile
from snowrig.connection import connect
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
        Action.CREATE: "[green]+ create[/green]",
        Action.UPDATE: "[yellow]~ update[/yellow]",
        Action.NOOP: "[dim]  no-op[/dim]",
        Action.ERROR: "[red]! error[/red]",
    }[action]


def _color_diff_line(line: str) -> str:
    """Applies Terraform-style coloring to one rendered diff line: green for
    an addition, red for a removal, yellow for an in-place change."""
    if line.startswith("+ "):
        return f"[green]{line}[/green]"
    if line.startswith("- "):
        return f"[red]{line}[/red]"
    if line.startswith("~ "):
        return f"[yellow]{line}[/yellow]"
    return line


@app.command()
def plan(
    manifest_dir: str = typer.Argument(..., help="Path to the manifest directory"),
    profile: str = "default",
) -> None:
    """Diff a manifest against live Snowflake state without changing anything."""
    plan_result = snowrig.plan(manifest_dir, profile=profile)
    if not plan_result:
        console.print(f"[yellow]No .yaml objects found under {manifest_dir}[/yellow]")
        return

    table = Table("Action", "Resource", "Object", "Changed fields")
    for change in plan_result:
        if change.diff:
            lines = []
            for fname, d in change.diff.items():
                if fname == "sql":
                    lines.append("sql: will be (re-)applied")
                    continue
                rendered = d.render()
                if any(rendered.startswith(p) for p in ("+ ", "- ", "~ ")):
                    lines.append(f"{fname}:")
                    lines.extend(f"  {_color_diff_line(part)}" for part in rendered.split("; "))
                else:
                    lines.append(f"{fname}: {rendered}")
            fields = "\n".join(lines)
        else:
            fields = change.error or ""
        if change.is_destructive:
            fields = f"[red]⚠ DESTRUCTIVE[/red]\n{fields}" if fields else "[red]⚠ DESTRUCTIVE[/red]"
        table.add_row(
            _action_style(change.action), change.key.resource, change.key.qualified_name, fields
        )
    console.print(table)

    n_create = sum(1 for c in plan_result if c.action == Action.CREATE)
    n_update = sum(1 for c in plan_result if c.action == Action.UPDATE)
    n_destructive = sum(1 for c in plan_result if c.is_destructive)
    n_error = sum(1 for c in plan_result if c.action == Action.ERROR)
    summary = f"Plan: [green]{n_create} to add[/green], [yellow]{n_update} to change[/yellow]"
    if n_destructive:
        summary += f", [red]{n_destructive} destructive (blocked without --allow-destructive)[/red]"
    if n_error:
        summary += f", [red]{n_error} error(s)[/red]"
    console.print(summary + ".")


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
    results = snowrig.apply(
        manifest_dir,
        profile=profile,
        dry_run=dry_run,
        stop_on_error=not continue_on_error,
        allow_destructive=allow_destructive,
    )

    if not results:
        console.print("[dim]Nothing to do — live state already matches the manifest.[/dim]")
        return

    console.print(f"Applied/attempted {len(results)} change(s){' (dry run)' if dry_run else ''}...")
    for change, error in results:
        label = f"{change.key.resource}:{change.key.qualified_name}"
        if change.blocked:
            console.print(f"  [yellow]BLOCKED[/yellow]  {label} — {change.blocked}")
        elif error:
            console.print(f"  [red]FAILED[/red]  {label} — {error}")
        elif dry_run:
            console.print(f"  [dim]WOULD APPLY[/dim]  {label}")
        else:
            console.print(f"  [green]APPLIED[/green]  {label}")


@app.command()
def resources() -> None:
    """List resource types currently supported by the typed object client."""
    from snowrig.resources.core_registry import RESOURCE_MODELS

    for name in sorted(RESOURCE_MODELS):
        console.print(f"  {name}")
    console.print("  [dim]procedure, function — via raw SQL (`sql:` in the manifest), not this list[/dim]")


@app.command()
def serve(
    config: str = typer.Option(None, help="Path to config.yaml (defaults to ~/.snowrig/config.yaml)"),
    host: str = typer.Option(
        "127.0.0.1", help="Bind address. Only use 0.0.0.0 behind your own auth/network layer."
    ),
    port: int = typer.Option(8420),
    token_env: str = typer.Option(
        "SNOWRIG_API_TOKEN", help="Env var holding the bearer token every request must present"
    ),
) -> None:
    """Serve a small REST API so other platforms can run SQL through a
    named profile over plain HTTP, without ever holding the private key
    themselves. Needs the 'api' extra: pip install snowrig[api]"""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Missing the 'api' extra.[/red] Install with: pip install snowrig[api]")
        raise typer.Exit(1)

    try:
        from snowrig.api.server import build_app
    except ImportError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not os.environ.get(token_env):
        console.print(
            f"[red]{token_env} is not set.[/red] Set it to a long random value before "
            f"starting the server — every request must present it as a Bearer token."
        )
        raise typer.Exit(1)

    if host not in ("127.0.0.1", "localhost"):
        console.print(
            f"[yellow]Binding to {host} exposes this API beyond localhost.[/yellow] "
            f"Make sure something in front of it (network policy, reverse proxy) restricts access."
        )

    config_path = Path(config) if config else None
    fastapi_app = build_app(config_path=config_path, token_env=token_env)
    console.print(f"[green]Serving on http://{host}:{port}[/green] (Ctrl+C to stop)")
    uvicorn.run(fastapi_app, host=host, port=port)


if __name__ == "__main__":
    app()