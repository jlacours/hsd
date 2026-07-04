"""CLI for HSD — human and scripting interface to the task board."""

import os
import subprocess
import sys
from pathlib import Path

import click

from hsd.core.db import Database, get_default_db_path
from hsd.core.models import REQUIRED_SUBMIT_SECTIONS, Task
from hsd.core.rules import (
    validate_transition,
    validate_submit_gate,
    validate_no_self_review,
)
from hsd.core.secret_scan import validate_no_secrets, scan_text, scan_dict
from hsd.render.markdown import render_task as render_md
from hsd.render.org import render_task as render_org
from hsd.migrate.importer import Migrator

PASS_DB = click.make_pass_decorator(Database, ensure=True)


@click.group()
@click.option("--db", default=None, help="Database path (default: XDG_DATA_HOME/hsd/hsd.db)")
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """HSD — Handoff Specification Database v2."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = Database(db)


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
    """Create a new task."""
    # Validate model note
    if source_model == "MODEL NOT EXPOSED" and not model_check_note:
        click.echo("Error: --model-check-note is required when source-model is 'MODEL NOT EXPOSED'", err=True)
        sys.exit(1)

    sections: dict[str, str] = {}
    for s in section:
        if "=" not in s:
            click.echo(f"Error: section must be key=value, got: {s}", err=True)
            sys.exit(1)
        key, _, val = s.partition("=")
        sections[key.strip()] = val.strip()

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
@click.option("--harness", required=True, help="Claiming harness name")
@click.option("--model", required=True, help="Claiming model identifier")
@PASS_DB
def claim(db: Database, slug_or_id: str, harness: str, model: str) -> None:
    """Claim a task (todo → in-progress)."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    # Validate transition
    ok, reason = validate_transition(task, "in-progress")
    if not ok:
        click.echo(f"Error: {reason}", err=True)
        sys.exit(1)

    result = db.claim_task(task.slug, harness, model)
    if result is None:
        click.echo("Error: task already claimed or not in todo stage", err=True)
        sys.exit(1)

    click.echo(f"Claimed task: {result.slug} (now in-progress, owned by {harness})")


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

    sections: dict[str, str] = {}
    for s in section:
        if "=" not in s:
            click.echo(f"Error: section must be key=value, got: {s}", err=True)
            sys.exit(1)
        key, _, val = s.partition("=")
        sections[key.strip()] = val.strip()

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


@cli.command()
@click.argument("slug_or_id")
@click.option("--section", "-s", multiple=True, required=True, help="section=content for required sections")
@PASS_DB
def submit(db: Database, slug_or_id: str, section: tuple[str, ...]) -> None:
    """Submit a task for review (in-progress → done)."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    sections: dict[str, str] = {}
    for s in section:
        if "=" not in s:
            click.echo(f"Error: section must be key=value, got: {s}", err=True)
            sys.exit(1)
        key, _, val = s.partition("=")
        sections[key.strip()] = val.strip()

    # Validate submit gate
    all_text = " ".join(sections.values())
    ok, err = validate_no_secrets(all_text)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    # Patch sections first
    task = db.update_task(task.slug, section_patches=sections)
    if task is None:
        click.echo("Error: task not found after update", err=True)
        sys.exit(1)

    ok, reason = validate_submit_gate(task)
    if not ok:
        click.echo(f"Error: submit gate: {reason}", err=True)
        sys.exit(1)

    ok, reason = validate_transition(task, "done")
    if not ok:
        click.echo(f"Error: {reason}", err=True)
        sys.exit(1)

    result = db.transition_task(
        task.slug, "done",
        actor_harness=task.owner_harness or "unknown",
        actor_model=task.owner_model or "unknown",
        note="submitted for review",
    )
    click.echo(f"Submitted task: {result.slug} (now in done)")


@cli.command()
@click.argument("slug_or_id")
@click.option("--reviewer-harness", required=True)
@click.option("--reviewer-model", required=True)
@click.option("--verdict", required=True, type=click.Choice(["accepted", "changes-requested", "human-revision-required"]))
@click.option("--findings", required=True)
@click.option("--disposition", required=True)
@PASS_DB
def review(db: Database, slug_or_id: str,
           reviewer_harness: str, reviewer_model: str,
           verdict: str, findings: str, disposition: str) -> None:
    """Review a task and set its next stage."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    all_text = " ".join([findings, disposition])
    ok, err = validate_no_secrets(all_text)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    # No self-review
    ok, warn = validate_no_self_review(task, reviewer_harness, reviewer_model)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)
    if warn:
        click.echo(f"Warning: {warn}")

    result, error = db.add_review(
        task.slug, reviewer_harness, reviewer_model,
        verdict, findings, disposition,
    )
    if error:
        click.echo(f"Error: {error}", err=True)
        sys.exit(1)
    click.echo(f"Reviewed task: {result.slug} (now in '{result.stage}')")


@cli.command()
@click.argument("slug_or_id")
@click.option("--reason", required=True)
@click.option("--exact-human-action", required=True)
@PASS_DB
def escalate(db: Database, slug_or_id: str, reason: str, exact_human_action: str) -> None:
    """Escalate a task to human revision."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    result = db.transition_task(
        task.slug, "to-be-revised-by-human",
        actor_harness=task.owner_harness or "unknown",
        actor_model=task.owner_model or "unknown",
        note=f"Escalated: {reason}. Human action: {exact_human_action}",
    )
    click.echo(f"Escalated task: {result.slug} (now in to-be-revised-by-human)")


@cli.command()
@click.argument("slug_or_id")
@click.option("--note", default="Resolved by human")
@PASS_DB
def resolve(db: Database, slug_or_id: str, note: str) -> None:
    """Resolve a human-revision task back to todo."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    result = db.transition_task(
        task.slug, "todo",
        actor_harness="human",
        actor_model="human",
        note=note,
    )
    click.echo(f"Resolved task: {result.slug} (now in todo)")


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


@cli.command()
@click.option("--source", default=None, help="V1 share directory path (default: ~/.harnesses_share_directory)")
@PASS_DB
def migrate(db: Database, source: str | None) -> None:
    """Migrate tasks from the v1 file-pair board into the database."""
    migrator = Migrator(db)
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


def main() -> None:
    cli()
