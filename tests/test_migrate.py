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
    def test_import_slug_collisions_preserve_both_tasks(self, db: Database, tmp_path):
        todo = tmp_path / "for-any-harness" / "todo"
        todo.mkdir(parents=True)
        for filename, objective in (
            ("Foo Bar.md", "First legacy task"),
            ("foo-bar.md", "Second legacy task"),
        ):
            (todo / filename).write_text(
                f"""# Handoff: {objective}

## Objective

{objective}

## Current State

Not imported.
"""
            )

        migrator = Migrator(db)
        first = migrator.migrate(str(tmp_path))
        second = migrator.migrate(str(tmp_path))

        assert first.imported == 2
        assert first.skipped == 0
        assert second.imported == 0
        assert second.skipped == 2
        tasks = db.list_tasks()
        assert {task.sections_dict()["objective"] for task in tasks} == {
            "First legacy task",
            "Second legacy task",
        }
        assert any(task.slug == "foo-bar" for task in tasks)
        assert any(task.slug.startswith("foo-bar-") for task in tasks)

    def test_import_normalizes_legacy_format(self, db: Database, tmp_path):
        todo = tmp_path / "for-any-harness" / "todo"
        todo.mkdir(parents=True)
        (todo / "Legacy Task 42.md").write_text(
            """# Handoff: Legacy Task

## Objective

Import this old handoff.
"""
        )

        result = Migrator(db).migrate(str(tmp_path))
        assert result.errors == 0
        assert result.imported == 1
        task = db.get_task("legacy-task-42")
        assert task is not None
        assert task.source_model == "MODEL NOT EXPOSED"
        assert task.model_check_note == "v1 import: source model metadata was not recorded"
        assert task.sections_dict()["current_state"].startswith("(imported")

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

    def test_migrate_infers_owner(self, db: Database, v1_board: Path):
        """Non-todo tasks get owner inferred from board directory name."""
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        task = db.get_task("completed-task")
        assert task is not None
        assert task.owner_harness == "codex"

    def test_migrate_preserves_timestamp(self, db: Database, v1_board: Path):
        """updated_at is set from the filename timestamp."""
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        task = db.get_task("completed-task")
        assert task is not None
        assert task.updated_at == "2026-07-03T10:00:00Z"

    def test_heal_backfills_owner(self, db: Database, v1_board: Path):
        """heal sets owner_harness for tasks imported without it."""
        # Import fresh (owner is now set during import, so simulate old import)
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        # Wipe owner to simulate pre-fix state
        db._conn().execute("UPDATE tasks SET owner_harness = NULL, owner_model = NULL WHERE slug = 'completed-task'")
        db._conn().commit()

        result = migrator.heal(str(v1_board))
        assert result.healed >= 1
        assert result.errors == 0

        task = db.get_task("completed-task")
        assert task.owner_harness == "codex"

    def test_heal_backfills_timestamp(self, db: Database, v1_board: Path):
        """heal sets updated_at from filename when it differs."""
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        # Wipe timestamp to simulate pre-fix state
        db._conn().execute(
            "UPDATE tasks SET updated_at = '2024-01-01T00:00:00Z' WHERE slug = 'completed-task'"
        )
        db._conn().commit()

        result = migrator.heal(str(v1_board))
        assert result.healed >= 1

        task = db.get_task("completed-task")
        assert task.updated_at == "2026-07-03T10:00:00Z"

    def test_heal_idempotent(self, db: Database, v1_board: Path):
        """heal can be re-run safely."""
        migrator = Migrator(db)
        migrator.migrate(str(v1_board))

        # Wipe owner to simulate pre-fix state
        db._conn().execute("UPDATE tasks SET owner_harness = NULL, owner_model = NULL WHERE slug = 'completed-task'")
        db._conn().commit()

        r1 = migrator.heal(str(v1_board))
        assert r1.healed >= 1

        r2 = migrator.heal(str(v1_board))
        assert r2.healed == 0  # already healed

    def test_heal_skips_nonexistent(self, db: Database, v1_board: Path):
        """heal silently skips files not in the DB."""
        migrator = Migrator(db)
        result = migrator.heal(str(v1_board))  # no import first
        assert result.healed == 0
        assert result.errors == 0
