"""Task state-flow CLI commands: claim, submit, review, escalate, resolve,
human-review.
"""

import sys
from pathlib import Path

import click

from hsd.core.db import Database
from hsd.core.rules import (
    validate_transition,
    validate_no_self_review,
)
from hsd.core.secret_scan import validate_no_secrets

from hsd.cli._shared import PASS_DB, _parse_sections, _resolve_task, cli


@cli.command()
@click.argument("slug_or_id")
@click.option("--harness", required=True, help="Claiming harness name")
@click.option("--model", required=True, help="Claiming model identifier")
@PASS_DB
def claim(db: Database, slug_or_id: str, harness: str, model: str) -> None:
    """Claim a task (todo -> in-progress)."""
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
@click.option("--section", "-s", multiple=True, required=True, help="section=content for required sections")
@click.option("--diff-file", default=None, help="Path to a diff file to attach ('-' for stdin)")
@click.option("--verify-cmd", default=None, help="Verification command to record")
@PASS_DB
def submit(db: Database, slug_or_id: str, section: tuple[str, ...],
           diff_file: str | None, verify_cmd: str | None) -> None:
    """Submit a task for review (in-progress -> done)."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    sections = _parse_sections(section)

    # Validate submit gate
    all_text = " ".join(sections.values())
    ok, err = validate_no_secrets(all_text)
    if not ok:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    diff: str | None = None
    if diff_file is not None:
        try:
            if diff_file == "-":
                diff = sys.stdin.read()
            else:
                diff = Path(diff_file).read_text()
        except OSError as e:
            click.echo(f"Error: could not read diff file '{diff_file}': {e}", err=True)
            sys.exit(1)

    try:
        result = db.submit_task_for_review(
            task.slug,
            actor_harness=task.owner_harness or "unknown",
            actor_model=task.owner_model or "unknown",
            sections=sections,
            diff=diff,
            verify_cmd=verify_cmd,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if result is None:
        click.echo("Error: task not found", err=True)
        sys.exit(1)
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
        click.echo(f"Error: {warn}", err=True)
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

    ok, deny_reason = validate_transition(task, "to-be-revised-by-human")
    if not ok:
        click.echo(f"Error: {deny_reason}", err=True)
        sys.exit(1)

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


@cli.command("human-review")
@click.argument("slug_or_id")
@click.option("--verdict", required=True, type=click.Choice(["accept", "revise"]))
@click.option("--note", default=None)
@PASS_DB
def human_review(db: Database, slug_or_id: str, verdict: str, note: str | None) -> None:
    """Record a human review verdict on a task in the 'reviewed' stage."""
    task = _resolve_task(db, slug_or_id)
    if task is None:
        return

    to_stage = "closed" if verdict == "accept" else "to-be-revised-by-human"

    ok, reason = validate_transition(task, to_stage)
    if not ok:
        click.echo(f"Error: {reason}", err=True)
        sys.exit(1)

    transition_note = f"human review: {verdict}"
    if note:
        transition_note += f" — {note}"

    result = db.transition_task(
        task.slug, to_stage,
        actor_harness="human",
        actor_model="human",
        note=transition_note,
    )
    click.echo(f"Reviewed task: {result.slug} (now in {to_stage})")
