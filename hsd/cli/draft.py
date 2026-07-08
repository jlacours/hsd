"""Task authoring CLI commands: create and update."""

import sys

import click

from hsd.core.db import Database
from hsd.core.secret_scan import validate_no_secrets
from hsd.render.markdown import render_task as render_md

from hsd.cli._shared import PASS_DB, _parse_sections, _resolve_task, cli


@cli.command()
@click.argument("slug")
@click.argument("title")
@click.option("--destination", default="any", help="Destination harness")
@click.option("--source-harness", required=True, help="Source harness name")
@click.option("--source-model", required=True, help="Source model identifier")
@click.option("--model-check-note", default=None, help="Note about model check")
@click.option("--author", default=None)
@click.option("--working-dir", default=None)
@click.option("--repository", default=None)
@click.option("--branch-commit", default=None)
@click.option("--tree-state", default=None)
@click.option("--section", "-s", multiple=True, help="section=content (can repeat)")
@PASS_DB
def create(db: Database, slug: str, title: str, destination: str,
           source_harness: str, source_model: str, model_check_note: str | None,
           author: str | None, working_dir: str | None, repository: str | None,
           branch_commit: str | None, tree_state: str | None,
           section: tuple[str, ...]) -> None:
    """Create a task whose TITLE excludes harness and model metadata."""
    # Validate model note
    if source_model == "MODEL NOT EXPOSED" and not model_check_note:
        click.echo("Error: --model-check-note is required when source-model is 'MODEL NOT EXPOSED'", err=True)
        sys.exit(1)

    sections = _parse_sections(section)

    # Secret scan
    all_text = " ".join(sections.values())
    ok, err = validate_no_secrets(all_text)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    try:
        task = db.create_task(
            slug=slug,
            title=title,
            destination=destination,
            sections=sections,
            source_harness=source_harness,
            source_model=source_model,
            model_check_note=model_check_note,
            author=author,
            working_dir=working_dir,
            repository=repository,
            branch_commit=branch_commit,
            tree_state=tree_state,
        )
    except Exception as e:
        click.echo(f"Error creating task: {e}", err=True)
        sys.exit(1)

    click.echo(f"Created task: {task.slug} (id={task.id})")
    click.echo(render_md(task))


@cli.command()
@click.argument("slug_or_id")
@click.option("--section", "-s", multiple=True, help="section=content (can repeat)")
@click.option("--status", default=None, help="Update status (queued|in-progress|blocked|complete)")
@PASS_DB
def update(db: Database, slug_or_id: str,
           section: tuple[str, ...], status: str | None) -> None:
    """Update a task's sections or status."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    sections = _parse_sections(section)

    all_text = " ".join(sections.values())
    ok, err = validate_no_secrets(all_text)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    result = db.update_task(task.slug, section_patches=sections or None, status=status)
    if result is None:
        click.echo("Error: task not found", err=True)
        sys.exit(1)
    click.echo(f"Updated task: {result.slug}")
