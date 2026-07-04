"""Tests for the v1 file-pair migration."""

import os
import tempfile
from pathlib import Path

import pytest

from hsd.core.db import Database
from hsd.migrate.importer import Migrator


@pytest.fixture
def v1_board():
    """Create a temporary v1 board structure with sample handoffs."""
    tmpdir = Path(tempfile.mkdtemp())
    for_any = tmpdir / "for-any-harness"
    for_codex = tmpdir / "for-codex"

    for d in [for_any / "todo", for_any / "in-progress",
              for_codex / "todo", for_codex / "done"]:
        d.mkdir(parents=True, exist_ok=True)

    # A typical todo handoff
    (for_any / "todo" / "20260704T142812Z--test-handoff.md").write_text("""# Handoff: Test Handoff — Codex — openai/gpt-5

## Metadata

| Field | Value |
|---|---|
| Source harness | Codex CLI |
| Model | openai/gpt-5 |
| Generated | 2026-07-04T14:28:12Z |
| Destination | all harnesses |
| Status | in-progress |
| Workflow stage | todo |

## Objective

Build the thing.

## Current State

Nothing works.

## Next Actions

1. Do stuff.
""")

    # A done handoff
    (for_codex / "done" / "20260703T100000Z--completed-task.md").write_text("""# Handoff: Completed Task — OpenCode — deepseek-v4

## Metadata

| Field | Value |
|---|---|
| Source harness | OpenCode |
| Model | deepseek-v4 |
| Destination | opencode |
| Status | complete |
| Workflow stage | done |

## Objective

Finish the feature.

## Work Completed

All done.

## Summary for Review

Works as expected.

## Commands and Verification

Tests pass.

## Next Actions

None.
""")

    yield tmpdir

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir)


class TestMigration:
    def test_migrate_imports_tasks(self, db: Database, v1_board: Path):
        migrator = Migrator(db)
        result = migrator.migrate(str(v1_board))
        assert result.imported == 2
        assert result.errors == 0

        # Verify tasks were imported
        tasks = db.list_tasks()
        assert len(tasks) >= 2

        test_task = db.get_task("test-handoff")
        assert test_task is not None
        assert "Test Handoff" in test_task.title

        completed = db.get_task("completed-task")
        assert completed is not None

    def test_migrate_idempotent(self, db: Database, v1_board: Path):
        migrator = Migrator(db)
        r1 = migrator.migrate(str(v1_board))
        r2 = migrator.migrate(str(v1_board))
        assert r1.imported == 2
        assert r2.imported == 0
        assert r2.skipped == 2

    def test_migrate_nonexistent_source(self, db: Database):
        migrator = Migrator(db)
        result = migrator.migrate("/nonexistent/path")
        assert result.errors == 1

    def test_migrate_extracts_sections(self, db: Database, v1_board: Path):
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        task = db.get_task("test-handoff")
        assert task is not None
        sections = task.sections_dict()
        assert "objective" in sections
        assert "current_state" in sections
        assert "next_actions" in sections

    def test_migrate_preserves_stage(self, db: Database, v1_board: Path):
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        task = db.get_task("completed-task")
        assert task is not None
        # The task should be in 'done' stage (matching the directory it was in)
        assert task.stage == "done"
