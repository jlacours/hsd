"""Operational CLI commands: migrate (v1 import) and serve (web dashboard)."""

import os
import subprocess
import sys

import click

from hsd.core.db import Database
from hsd.migrate.importer import Migrator

from hsd.cli._shared import PASS_DB, cli


@cli.command()
@click.option("--source", default=None, help="V1 share directory path (default: ~/.harnesses_share_directory)")
@click.option("--heal", is_flag=True, help="Backfill owner and timestamps for already-imported tasks")
@PASS_DB
def migrate(db: Database, source: str | None, heal: bool) -> None:
    """Migrate tasks from the v1 file-pair board into the database."""
    migrator = Migrator(db)
    if heal:
        result = migrator.heal(source)
        click.echo(f"Heal complete:")
        click.echo(f"  Healed: {result.healed}")
        click.echo(f"  Errors: {result.errors}")
        if result.error_details:
            click.echo("  Error details:")
            for err in result.error_details:
                click.echo(f"    - {err}")
    else:
        result = migrator.migrate(source)
        click.echo(f"Migration complete:")
        click.echo(f"  Imported: {result.imported}")
        click.echo(f"  Skipped (already exist): {result.skipped}")
        click.echo(f"  Errors: {result.errors}")
        if result.error_details:
            click.echo("  Error details:")
            for err in result.error_details:
                click.echo(f"    - {err}")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
@click.option("--port", default=8737, help="Port to bind (default: 8737)")
@PASS_DB
def serve(db: Database, host: str, port: int) -> None:
    """Start the web dashboard server."""
    click.echo(f"Starting web dashboard at http://{host}:{port}")
    cmd = [sys.executable, "-m", "uvicorn", "hsd.web.app:app", "--host", host, "--port", str(port)]
    os.environ["HSD_DB_PATH"] = db.db_path
    subprocess.run(cmd, check=True)
