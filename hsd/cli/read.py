"""Read-only CLI commands: ls, show, stats, export."""

from pathlib import Path

import click

from hsd.core.db import Database
from hsd.render.markdown import render_task as render_md
from hsd.render.org import render_task as render_org

from hsd.cli._shared import PASS_DB, _resolve_task, cli


@cli.command()
@click.option("--stage", default=None, help="Filter by stage")
@click.option("--harness", default=None, help="Filter by owner harness")
@click.option("--destination", default=None, help="Filter by destination")
@PASS_DB
def ls(db: Database, stage: str | None, harness: str | None, destination: str | None) -> None:
    """List tasks."""
    tasks = db.list_tasks(harness=harness, stage=stage, destination=destination)
    if not tasks:
        click.echo("No tasks found.")
        return

    click.echo(f"{'SLUG':<45} {'STAGE':<25} {'STATUS':<15} {'OWNER':<20} {'UPDATED'}")
    click.echo("-" * 120)
    for t in tasks:
        owner = t.owner_harness or "-"
        click.echo(f"{t.slug:<45} {t.stage:<25} {t.status:<15} {owner:<20} {t.updated_at}")


@cli.command()
@click.argument("slug_or_id")
@PASS_DB
def show(db: Database, slug_or_id: str) -> None:
    """Show full task details."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return
    click.echo(render_md(task))


@cli.command()
@click.argument("slug_or_id")
@click.option("--format", "-f", "fmt", type=click.Choice(["md", "org"]), default="md")
@click.option("--path", "-p", default=None, help="Output path (default: stdout)")
@PASS_DB
def export(db: Database, slug_or_id: str, fmt: str, path: str | None) -> None:
    """Export a task as markdown or org-mode."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    if fmt == "md":
        content = render_md(task)
    else:
        content = render_org(task)

    if path:
        Path(path).write_text(content)
        click.echo(f"Exported to {path}")
    else:
        click.echo(content)


@cli.command()
@PASS_DB
def stats(db: Database) -> None:
    """Show board statistics."""
    s = db.board_stats()
    click.echo("Board Statistics:")
    click.echo(f"  Total tasks: {s['total']}")
    click.echo("  By stage:")
    for stage, count in sorted(s["by_stage"].items()):
        click.echo(f"    {stage}: {count}")
