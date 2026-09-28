"""Shared CLI primitives: the click group, the database pass decorator,
task resolution, and section-argument parsing used across command modules.
"""

import sys

import click

from hsd.core.db import Database, get_default_db_path
from hsd.core.models import Task

PASS_DB = click.make_pass_decorator(Database, ensure=True)


@click.group()
@click.option(
    "--db",
    default=None,
    help="Database path (default: HSD_DB_PATH, otherwise XDG_DATA_HOME/hsd/hsd.db)",
)
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """HSD — Handoff Specification Database v2."""
    # The TUI owns its short-lived connections and must be able to report a
    # locked database in the interface, so pass it a path without opening DB.
    if ctx.invoked_subcommand == "tui":
        ctx.obj = db if db is not None else get_default_db_path()
    else:
        ctx.obj = Database(db)


def _resolve_task(db: Database, slug_or_id: str) -> Task | None:
    """Resolve a slug or ID to a Task, printing error and exiting if not found."""
    try:
        task_id = int(slug_or_id)
        task = db.get_task(task_id)
    except ValueError:
        task = db.get_task(slug_or_id)

    if task is None:
        click.echo(f"Error: task '{slug_or_id}' not found", err=True)
        sys.exit(1)
    return task


def _parse_sections(section: tuple[str, ...]) -> dict[str, str]:
    """Parse repeated ``-s key=value`` options into a dict.

    Exits with an error message if any option lacks an ``=`` separator,
    matching the original inline behaviour.
    """
    sections: dict[str, str] = {}
    for s in section:
        if "=" not in s:
            click.echo(f"Error: section must be key=value, got: {s}", err=True)
            sys.exit(1)
        key, _, val = s.partition("=")
        sections[key.strip()] = val.strip()
    return sections
