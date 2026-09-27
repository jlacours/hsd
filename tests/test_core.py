"""Tests for the core database layer and domain rules."""

import concurrent.futures
import sqlite3
import threading

import pytest

from hsd.core.db import Database
from hsd.core.db_schema import SCHEMA_SQL
from hsd.core.models import ALLOWED_TRANSITIONS, REQUIRED_SUBMIT_SECTIONS
from hsd.core.rules import (
    validate_transition,
    validate_submit_gate,
    validate_no_self_review,
    validate_model_note,
)
from hsd.core.secret_scan import validate_no_secrets, scan_text


class TestCreateTask:
    def test_create_and_retrieve(self, db: Database):
        task = db.create_task(
            slug="my-task",
            title="My Task",
            destination="opencode",
            sections={"objective": "Do the thing", "current_state": "Initial state"},
            source_harness="opencode",
            source_model="deepseek-v4",
        )
        assert task.id is not None
        assert task.slug == "my-task"
        assert task.title == "My Task"
        assert task.stage == "todo"
        assert task.status == "queued"
        assert task.destination == "opencode"
        assert task.sections_dict()["objective"] == "Do the thing"
        assert task.sections_dict()["current_state"] == "Initial state"

        # Retrieve by slug
        t2 = db.get_task("my-task")
        assert t2 is not None
        assert t2.id == task.id

        # Retrieve by ID
        t3 = db.get_task(task.id)
        assert t3 is not None
        assert t3.slug == "my-task"

    def test_server_stamped_timestamps(self, db: Database):
        task = db.create_task(
            slug="timestamps",
            title="Check stamps",
            destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="test",
            source_model="test",
        )
        assert task.created_at.endswith("Z")
        assert task.updated_at.endswith("Z")
        assert task.created_at == task.updated_at

    def test_duplicate_slug_raises(self, db: Database):
        db.create_task(
            slug="dup", title="First", destination="any",
            sections={"objective": "a", "current_state": "Initial state"}, source_harness="h", source_model="m",
        )
        with pytest.raises(Exception):
            db.create_task(
                slug="dup", title="Second", destination="any",
                sections={"objective": "b", "current_state": "Initial state"}, source_harness="h", source_model="m",
            )

    def test_invalid_section_name_raises(self, db: Database):
        with pytest.raises(ValueError, match="invalid section name"):
            db.create_task(
                slug="bad-section", title="Bad", destination="any",
                sections={"nonexistent": "content"},
                source_harness="h", source_model="m",
            )

    @pytest.mark.parametrize(
        ("slug", "sections", "message"),
        [
            (
                "Bad Slug",
                {"objective": "Do it", "current_state": "Not done"},
                "Invalid slug",
            ),
            ("missing-current", {"objective": "Do it"}, "current_state"),
            (
                "empty-objective",
                {"objective": "  ", "current_state": "Not done"},
                "objective",
            ),
        ],
    )
    def test_creation_format_is_enforced(self, db, slug, sections, message):
        with pytest.raises(ValueError, match=message):
            db.create_task(
                slug=slug,
                title="Canonical task",
                destination="any",
                sections=sections,
                source_harness="pytest",
                source_model="test-model",
            )

    def test_database_schema_rejects_invalid_slug(self, db):
        with pytest.raises(sqlite3.IntegrityError, match="canonical task metadata"):
            db._conn().execute(
                """INSERT INTO tasks
                   (slug, title, destination, stage, status, source_harness,
                    source_model, herdr_session, created_at, updated_at)
                   VALUES (?, ?, ?, 'todo', 'queued', ?, ?, ?, ?, ?)""",
                (
                    "Bad Slug",
                    "Invalid raw task",
                    "any",
                    "pytest",
                    "test-model",
                    "hsd-invalid-raw",
                    "2026-07-31T00:00:00Z",
                    "2026-07-31T00:00:00Z",
                ),
            )

    def test_database_requires_creation_sections_at_commit(self, db):
        db._conn().execute(
            """INSERT INTO tasks
               (slug, title, destination, stage, status, source_harness,
                source_model, herdr_session, created_at, updated_at)
               VALUES (?, ?, ?, 'todo', 'queued', ?, ?, ?, ?, ?)""",
            (
                "raw-without-sections",
                "Raw task",
                "any",
                "pytest",
                "test-model",
                "hsd-raw-no-sections",
                "2026-07-31T00:00:00Z",
                "2026-07-31T00:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db._conn().commit()
        db._conn().rollback()

    def test_required_sections_cannot_be_emptied(self, db):
        task = db.create_task(
            slug="preserve-format",
            title="Preserve Format",
            destination="any",
            sections={"objective": "Do it", "current_state": "Not done"},
            source_harness="pytest",
            source_model="test-model",
        )
        with pytest.raises(ValueError, match="objective"):
            db.update_task(task.slug, section_patches={"objective": "  "})

    def test_plan_update_uses_core_secret_validation(self, db):
        task = db.create_task(
            slug="safe-plan",
            title="Safe Plan",
            destination="any",
            sections={"objective": "Plan safely", "current_state": "No plan"},
            source_harness="pytest",
            source_model="test-model",
        )
        with pytest.raises(ValueError, match="Secret scan"):
            db.update_plan_if_current(
                task.slug,
                "",
                "AWS key: AKIAABCDEFGHIJKLMNOP",
            )
        assert "plan" not in db.get_task(task.slug).sections_dict()

    def test_plan_is_a_first_class_section(self, db: Database):
        task = db.create_task(
            slug="planned-task",
            title="Planned Task",
            destination="any",
            sections={
                "objective": "Do the thing",
                "current_state": "It is not done",
                "plan": "# Plan\n\n1. Do the thing.",
            },
            source_harness="human-web",
            source_model="human",
        )
        assert task.sections_dict()["plan"].startswith("# Plan")

    def test_model_not_exposed_validation(self):
        ok, err = validate_model_note("MODEL NOT EXPOSED", None)
        assert not ok
        assert "model_check_note" in err

        ok, err = validate_model_note("MODEL NOT EXPOSED", "checked via env var")
        assert ok

        ok, err = validate_model_note("claude-4", None)
        assert ok


class TestClaimTask:
    def test_claim_success(self, db: Database, sample_task):
        result = db.claim_task(sample_task.slug, "opencode", "deepseek-v4")
        assert result is not None
        assert result.stage == "in-progress"
        assert result.status == "in-progress"
        assert result.owner_harness == "opencode"
        assert result.owner_model == "deepseek-v4"

    def test_claim_already_claimed(self, db: Database, sample_task):
        db.claim_task(sample_task.slug, "opencode", "deepseek-v4")
        result = db.claim_task(sample_task.slug, "codex", "gpt-5")
        assert result is None  # should fail

    def test_claim_nonexistent(self, db: Database):
        result = db.claim_task("nonexistent", "h", "m")
        assert result is None

    def test_claim_race(self, db: Database):
        """Two concurrent claimers: exactly one should win."""
        db.create_task(
            slug="race-task", title="Race", destination="any",
            sections={"objective": "race test", "current_state": "Initial state"},
            source_harness="test", source_model="test",
        )

        results: list = []
        errors: list = []

        def claimer(name: str):
            try:
                result = db.claim_task("race-task", name, f"model-{name}")
                results.append((name, result is not None))
            except Exception as e:
                errors.append((name, str(e)))

        threads = [
            threading.Thread(target=claimer, args=("harness-a",)),
            threading.Thread(target=claimer, args=("harness-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"
        winners = [name for name, won in results if won]
        assert len(winners) == 1, f"Expected exactly 1 winner, got {winners} (results: {results})"


class TestTransitions:
    def _make_in_progress(self, db, slug="trans-test"):
        task = db.create_task(
            slug=slug, title="Transitions", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "opencode", "deepseek-v4")
        return db.get_task(task.slug)

    def test_todo_to_in_progress(self, db, sample_task):
        assert validate_transition(sample_task, "in-progress") == (True, "")

    def test_in_progress_to_done(self, db):
        task = self._make_in_progress(db)
        assert validate_transition(task, "done") == (True, "")

    def test_done_to_reviewed(self, db):
        task = self._make_in_progress(db)
        db.transition_task(task.slug, "done", "opencode", "deepseek-v4", note="done")
        task = db.get_task(task.slug)
        ok, _ = validate_transition(task, "reviewed")
        assert ok

    def test_done_to_todo(self, db):
        task = self._make_in_progress(db)
        db.transition_task(task.slug, "done", "opencode", "deepseek-v4")
        task = db.get_task(task.slug)
        assert validate_transition(task, "todo") == (True, "")

    def test_illegal_transition(self, db):
        task = db.create_task(
            slug="illegal", title="Illegal", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        ok, reason = validate_transition(task, "reviewed")
        assert not ok
        assert "not allowed" in reason

    def test_escalate_to_human(self, db):
        task = self._make_in_progress(db)
        result = db.transition_task(
            task.slug, "to-be-revised-by-human",
            "opencode", "deepseek-v4", note="stuck",
        )
        assert result.stage == "to-be-revised-by-human"

    def test_resolve_human(self, db):
        task = self._make_in_progress(db)
        db.transition_task(task.slug, "to-be-revised-by-human", "opencode", "deepseek-v4")
        result = db.transition_task(task.slug, "todo", "human", "human", note="fixed")
        assert result.stage == "todo"
        assert result.owner_harness is None
        assert result.owner_model is None
        assert result.destination == "any"

        claimed = db.claim_task(result.slug, "codex", "gpt-5.5")
        assert claimed is not None
        assert claimed.owner_harness == "codex"

    def test_transition_records_audit(self, db):
        task = self._make_in_progress(db)
        db.transition_task(task.slug, "done", "opencode", "deepseek-v4", note="all done")
        task = db.get_task(task.slug)
        assert len(task.transitions) == 3  # create + claim + submit
        last = task.transitions[-1]
        assert last.to_stage == "done"
        assert last.note == "all done"

    def test_transition_update_timestamp(self, db):
        task = self._make_in_progress(db)
        before = task.updated_at
        db.transition_task(task.slug, "done", "opencode", "deepseek-v4")
        task = db.get_task(task.slug)
        assert task.updated_at >= before


    def test_transition_task_enforces_illegal_move(self, db):
        """transition_task raises ValueError for illegal transitions."""
        task = db.create_task(
            slug="illegal-move", title="Illegal", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        with pytest.raises(ValueError, match="not allowed|Transition"):
            db.transition_task(task.slug, "reviewed", "h", "m")

    def test_create_task_with_stage_and_updated_at(self, db):
        """create_task accepts optional stage and updated_at params."""
        import datetime
        ts = "2026-07-04T10:00:00Z"
        task = db.create_task(
            slug="with-params", title="With Params", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
            stage="done", updated_at=ts,
        )
        assert task.stage == "done"
        assert task.updated_at == ts


class TestSubmitGate:
    def _make_in_progress_with_sections(self, db, sections: dict | None = None):
        task = db.create_task(
            slug="gate-test", title="Gate Test", destination="any",
            sections=sections or {"objective": "test", "current_state": "works"},
            source_harness="h", source_model="m",
        )
        return db.claim_task(task.slug, "opencode", "deepseek-v4")

    def test_submit_gate_passes(self, db):
        task = self._make_in_progress_with_sections(db)
        sections = {s: "some content" for s in REQUIRED_SUBMIT_SECTIONS}
        db.update_task(task.slug, section_patches=sections)
        task = db.get_task(task.slug)
        ok, _ = validate_submit_gate(task)
        assert ok

    def test_submit_gate_missing_sections(self, db):
        task = self._make_in_progress_with_sections(db)
        ok, reason = validate_submit_gate(task)
        assert not ok
        assert "missing" in reason

    def test_submit_gate_empty_sections(self, db):
        task = self._make_in_progress_with_sections(db)
        sections = {s: "" for s in REQUIRED_SUBMIT_SECTIONS}
        db.update_task(task.slug, section_patches=sections)
        task = db.get_task(task.slug)
        ok, reason = validate_submit_gate(task)
        assert not ok
        assert "empty" in reason


class TestNoSelfReview:
    def test_rejects_same_harness(self, db):
        task = db.create_task(
            slug="self-review", title="Self Review", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "opencode", "deepseek-v4")
        task = db.get_task(task.slug)
        ok, err = validate_no_self_review(task, "opencode", "gpt-5")
        assert not ok
        assert "self-review" in err

    def test_allows_different_harness(self, db):
        task = db.create_task(
            slug="cross-review", title="Cross", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        task = db.get_task(task.slug)
        ok, warn = validate_no_self_review(task, "opencode", "gpt-5")
        assert ok  # allowed, but warned
        assert "warning" in warn

    def test_allows_different_harness_and_model(self, db):
        task = db.create_task(
            slug="proper-review", title="Proper", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        task = db.get_task(task.slug)
        ok, warn = validate_no_self_review(task, "opencode", "deepseek-v4")
        assert ok
        assert not warn


class TestSecretScan:
    def test_rejects_pem_key(self):
        fake_key = "-----BEGIN RSA " + "PRIVATE KEY-----\nABCD\n-----END RSA PRIVATE KEY-----"
        ok, err = validate_no_secrets(fake_key)
        assert not ok
        assert "PEM" in err

    def test_rejects_github_pat(self):
        fake_pat = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
        ok, err = validate_no_secrets(f"use {fake_pat}")
        assert not ok
        assert "ghp_" in err

    def test_rejects_aws_key(self):
        fake_key = "AKIA" + "0123456789ABCDEF"
        ok, err = validate_no_secrets(fake_key)
        assert not ok
        assert "AKIA" in err

    def test_rejects_jwt(self):
        ok, err = validate_no_secrets("eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoxfQ.abc123def456ghi789jkl")
        assert not ok
        assert "JWT" in err

    def test_allows_clean_text(self):
        ok, err = validate_no_secrets("This is a normal task handoff with no secrets.")
        assert ok


class TestListTasks:
    def test_list_all(self, db):
        db.create_task(slug="a", title="A", destination="any", sections={"objective": "a", "current_state": "Initial state"}, source_harness="h", source_model="m")
        db.create_task(slug="b", title="B", destination="opencode", sections={"objective": "b", "current_state": "Initial state"}, source_harness="h", source_model="m")
        tasks = db.list_tasks()
        assert len(tasks) >= 2

    def test_filter_by_harness(self, db):
        db.create_task(slug="c1", title="C1", destination="any", sections={"objective": "c1", "current_state": "Initial state"}, source_harness="opencode", source_model="m")
        db.create_task(slug="c2", title="C2", destination="any", sections={"objective": "c2", "current_state": "Initial state"}, source_harness="codex", source_model="m")
        db.claim_task("c1", "opencode", "m")
        tasks = db.list_tasks(harness="opencode")
        assert all(t.owner_harness == "opencode" for t in tasks)

    def test_filter_by_stage(self, db, sample_task):
        tasks = db.list_tasks(stage="todo")
        assert sample_task.slug in [t.slug for t in tasks]


class TestBoardStats:
    def test_stats(self, db, sample_task):
        stats = db.board_stats()
        assert stats["total"] >= 1
        assert "todo" in stats["by_stage"]


class TestAgentProfiles:
    def test_lists_all_workflow_purposes(self, db: Database):
        profiles = db.list_agent_profiles()
        assert [profile.purpose for profile in profiles] == [
            "planning", "coding", "reviewing", "bugs", "maintenance",
        ]
        assert all(profile.provider == "" for profile in profiles)

    def test_profile_round_trip(self, db: Database):
        saved = db.set_agent_profile("reviewing", "openai", "gpt-5.6")
        assert saved.provider == "openai"
        assert saved.model == "gpt-5.6"

        reopened = Database(db.db_path)
        profile = next(
            item for item in reopened.list_agent_profiles()
            if item.purpose == "reviewing"
        )
        assert profile.provider == "openai"
        assert profile.model == "gpt-5.6"

    def test_invalid_profile_purpose_is_rejected(self, db: Database):
        with pytest.raises(ValueError, match="invalid agent purpose"):
            db.set_agent_profile("vibes", "openai", "gpt-whatever")

    def test_existing_database_gains_plan_section(self, tmp_path):
        db_path = tmp_path / "legacy-sections.db"
        old_schema = SCHEMA_SQL.replace("'objective','plan'", "'objective'")
        connection = sqlite3.connect(db_path)
        connection.executescript(old_schema)
        connection.close()

        migrated = Database(str(db_path))
        task = migrated.create_task(
            slug="post-migration-plan",
            title="Post Migration Plan",
            destination="any",
            sections={
                "objective": "Verify plan migration",
                "current_state": "The database uses a legacy section constraint",
                "plan": "The migrated schema accepts plans.",
            },
            source_harness="human-web",
            source_model="human",
        )
        assert task.sections_dict()["plan"] == "The migrated schema accepts plans."

    def test_existing_tasks_gain_persisted_herdr_sessions(self, tmp_path):
        db_path = tmp_path / "legacy-herdr.db"
        old_schema = SCHEMA_SQL.replace(
            "    herdr_session   TEXT NOT NULL UNIQUE,\n",
            "",
        )
        connection = sqlite3.connect(db_path)
        connection.executescript(old_schema)
        connection.execute(
            """INSERT INTO tasks
               (slug, title, destination, stage, status, source_harness,
                source_model, created_at, updated_at)
               VALUES ('legacy-herdr', 'Legacy', 'any', 'todo', 'queued',
                       'human', 'human', '2026-07-31T00:00:00Z',
                       '2026-07-31T00:00:00Z')"""
        )
        connection.commit()
        connection.close()

        migrated = Database(str(db_path))
        session = migrated.get_task("legacy-herdr").herdr_session
        assert session.startswith("hsd-")
        assert Database(str(db_path)).get_task("legacy-herdr").herdr_session == session
        assert migrated.get_task("legacy-herdr").sections_dict() == {
            "objective": "(legacy task — objective not recorded)",
            "current_state": "(legacy task — current state not recorded)",
        }
        columns = {
            row["name"]: row
            for row in migrated._conn().execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert columns["herdr_session"]["notnull"] == 1
        unique_columns = {
            tuple(
                row["name"]
                for row in migrated._conn().execute(
                    f"PRAGMA index_info({index['name']!r})"
                ).fetchall()
            )
            for index in migrated._conn().execute("PRAGMA index_list(tasks)").fetchall()
            if index["unique"] == 1
        }
        assert ("herdr_session",) in unique_columns

    def test_legacy_noncanonical_metadata_does_not_block_migration(self, tmp_path):
        db_path = tmp_path / "legacy-metadata.db"
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                destination TEXT NOT NULL,
                owner_harness TEXT,
                owner_model TEXT,
                stage TEXT NOT NULL CHECK (stage IN
                    ('todo','in-progress','done','reviewed',
                     'to-be-revised-by-human','closed')),
                status TEXT NOT NULL CHECK (status IN
                    ('queued','in-progress','blocked','complete')),
                source_harness TEXT NOT NULL,
                source_model TEXT NOT NULL,
                model_check_note TEXT,
                author TEXT,
                working_dir TEXT,
                repository TEXT,
                branch_commit TEXT,
                tree_state TEXT,
                diff TEXT,
                verify_cmd TEXT,
                herdr_session TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sections (
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (task_id, name)
            );
            INSERT INTO tasks
                (id, slug, title, destination, stage, status, source_harness,
                 source_model, herdr_session, created_at, updated_at)
            VALUES
                (7, 'Legacy Task', 'Legacy', 'any', 'todo', 'queued',
                 'old-harness', 'old-model', 'hsd-legacy-metadata',
                 '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z');
            INSERT INTO sections (task_id, name, content)
            VALUES (7, 'objective', 'Preserve this task'),
                   (7, 'current_state', 'Legacy metadata');
            """
        )
        connection.commit()
        connection.close()

        migrated = Database(str(db_path))
        assert migrated.get_task("Legacy Task").title == "Legacy"
        trigger_names = {
            row[0]
            for row in migrated._conn().execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "trg_tasks_canonical_insert" in trigger_names


class TestRender:
    from hsd.render.markdown import render_task as render_md
    from hsd.render.org import render_task as render_org

    def test_md_renders(self, db, sample_task):
        from hsd.render.markdown import render_task
        output = render_task(sample_task)
        assert "# Handoff:" in output
        assert "test-harness" in output
        assert "## Objective" in output
        assert "Test the system" in output

    def test_org_renders(self, db, sample_task):
        from hsd.render.org import render_task
        output = render_task(sample_task)
        assert "#+title:" in output
        assert "test-harness" in output


class TestReviews:
    def test_add_review_accepted(self, db):
        task = db.create_task(
            slug="review-task", title="Review", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "accepted", "Looks good", "approved",
        )
        assert error is None
        assert result.stage == "reviewed"

    def test_add_review_changes_requested(self, db):
        task = db.create_task(
            slug="changes", title="Changes", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "changes-requested", "Needs work", "revise",
        )
        assert error is None
        assert result.stage == "todo"

    def test_add_review_human_revision(self, db):
        task = db.create_task(
            slug="human", title="Human", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "human-revision-required", "Needs human", "escalate",
        )
        assert error is None
        assert result.stage == "to-be-revised-by-human"

    def test_review_not_in_done(self, db, sample_task):
        result, error = db.add_review(
            sample_task.slug, "opencode", "deepseek-v4",
            "accepted", "fine", "ok",
        )
        assert error is not None
        assert "only tasks in 'done' stage can be reviewed" in error

    def test_changes_requested_clears_owner(self, db):
        """changes-requested clears owner_harness/owner_model and sets destination to original owner."""
        task = db.create_task(
            slug="cr-owner", title="CR Owner", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "changes-requested", "Needs work", "revise",
        )
        assert error is None
        assert result.stage == "todo"
        assert result.owner_harness is None
        assert result.owner_model is None
        assert result.destination == "codex"

    def test_human_revision_clears_owner(self, db):
        """human-revision-required clears owner_harness/owner_model, destination unchanged."""
        task = db.create_task(
            slug="hr-owner", title="HR Owner", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "human-revision-required", "Need human", "escalate",
        )
        assert error is None
        assert result.stage == "to-be-revised-by-human"
        assert result.owner_harness is None
        assert result.owner_model is None


class TestClosedStage:
    def _make_reviewed(self, db, slug="closed-flow"):
        task = db.create_task(
            slug=slug, title="Closed Flow", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        db.transition_task(task.slug, "done", "codex", "gpt-5")
        result, error = db.add_review(
            task.slug, "opencode", "deepseek-v4",
            "accepted", "Looks good", "approved",
        )
        assert error is None
        assert result.stage == "reviewed"
        return result

    def test_reviewed_to_closed_allowed(self, db):
        task = self._make_reviewed(db)
        assert validate_transition(task, "closed") == (True, "")
        result = db.transition_task(
            task.slug, "closed", "human", "human", note="human review: accept",
        )
        assert result.stage == "closed"
        assert result.status == "complete"

    def test_reviewed_to_human_revision_allowed(self, db):
        task = self._make_reviewed(db, slug="closed-flow-2")
        assert validate_transition(task, "to-be-revised-by-human") == (True, "")
        result = db.transition_task(
            task.slug, "to-be-revised-by-human", "human", "human",
            note="human review: revise",
        )
        assert result.stage == "to-be-revised-by-human"

    def test_closed_has_no_outgoing_transitions(self, db):
        task = self._make_reviewed(db, slug="closed-flow-3")
        closed = db.transition_task(task.slug, "closed", "human", "human")
        assert closed.stage == "closed"

        for target in ("todo", "in-progress", "done", "reviewed", "to-be-revised-by-human"):
            ok, reason = validate_transition(closed, target)
            assert not ok
            assert "not allowed" in reason

        with pytest.raises(ValueError, match="not allowed"):
            db.transition_task(closed.slug, "todo", "human", "human")

        # the rejected attempt must not have mutated the task
        unchanged = db.get_task(closed.slug)
        assert unchanged.stage == "closed"


class TestSubmitArtifacts:
    def _make_in_progress(self, db, slug="artifact-task"):
        task = db.create_task(
            slug=slug, title="Artifact Task", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        return db.get_task(task.slug)

    def test_submit_persists_diff_and_verify_cmd(self, db):
        task = self._make_in_progress(db)
        result = db.transition_task(
            task.slug, "done", "codex", "gpt-5",
            diff="diff --git a/f b/f\n+line", verify_cmd="pytest tests/",
        )
        assert result.diff == "diff --git a/f b/f\n+line"
        assert result.verify_cmd == "pytest tests/"

    def test_resubmit_overwrites_diff_and_verify_cmd(self, db):
        task = self._make_in_progress(db, slug="resubmit-task")
        db.transition_task(
            task.slug, "done", "codex", "gpt-5",
            diff="v1 diff", verify_cmd="pytest v1",
        )
        # simulate changes-requested: back to todo, re-claim, resubmit
        db.transition_task(task.slug, "todo", "human", "human", note="changes requested")
        db.claim_task(task.slug, "codex", "gpt-5")
        result = db.transition_task(
            task.slug, "done", "codex", "gpt-5",
            diff="v2 diff", verify_cmd="pytest v2",
        )
        assert result.diff == "v2 diff"
        assert result.verify_cmd == "pytest v2"

    def test_resubmit_omitted_leaves_diff_and_verify_cmd_untouched(self, db):
        task = self._make_in_progress(db, slug="omit-task")
        db.transition_task(
            task.slug, "done", "codex", "gpt-5",
            diff="original diff", verify_cmd="original verify",
        )
        db.transition_task(task.slug, "todo", "human", "human")
        db.claim_task(task.slug, "codex", "gpt-5")
        result = db.transition_task(task.slug, "done", "codex", "gpt-5")
        assert result.diff == "original diff"
        assert result.verify_cmd == "original verify"

    def test_submit_with_secret_in_diff_rejected(self, db):
        task = self._make_in_progress(db, slug="secret-task")
        fake_key = "AKIA" + "0123456789ABCDEF"
        with pytest.raises(ValueError, match="Secret scan blocked"):
            db.transition_task(
                task.slug, "done", "codex", "gpt-5",
                diff=f"key: {fake_key}",
            )
        unchanged = db.get_task(task.slug)
        assert unchanged.stage == "in-progress"
        assert unchanged.diff is None


class TestLegacyMigration:
    """A pre-'closed' (v2.0, five-stage) database must upgrade transparently."""

    OLD_SCHEMA_SQL = """
        CREATE TABLE tasks (
            id              INTEGER PRIMARY KEY,
            slug            TEXT NOT NULL UNIQUE,
            title           TEXT NOT NULL,
            destination     TEXT NOT NULL,
            owner_harness   TEXT,
            owner_model     TEXT,
            stage           TEXT NOT NULL CHECK (stage IN
                ('todo','in-progress','done','reviewed','to-be-revised-by-human')),
            status          TEXT NOT NULL CHECK (status IN
                ('queued','in-progress','blocked','complete')),
            source_harness  TEXT NOT NULL,
            source_model    TEXT NOT NULL,
            model_check_note TEXT,
            author          TEXT,
            working_dir     TEXT,
            repository      TEXT,
            branch_commit   TEXT,
            tree_state      TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE sections (
            task_id   INTEGER NOT NULL REFERENCES tasks(id),
            name      TEXT NOT NULL,
            content   TEXT NOT NULL,
            PRIMARY KEY (task_id, name)
        );

        CREATE TABLE transitions (
            id            INTEGER PRIMARY KEY,
            task_id       INTEGER NOT NULL REFERENCES tasks(id),
            at            TEXT NOT NULL,
            actor_harness TEXT NOT NULL,
            actor_model   TEXT NOT NULL,
            from_stage    TEXT,
            to_stage      TEXT NOT NULL,
            note          TEXT
        );

        CREATE TABLE reviews (
            id                INTEGER PRIMARY KEY,
            task_id           INTEGER NOT NULL REFERENCES tasks(id),
            at                TEXT NOT NULL,
            reviewer_harness  TEXT NOT NULL,
            reviewer_model    TEXT NOT NULL,
            verdict           TEXT NOT NULL,
            findings          TEXT NOT NULL,
            disposition       TEXT NOT NULL
        );
    """

    def test_legacy_five_stage_schema_migrates_on_open(self, tmp_path):
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(self.OLD_SCHEMA_SQL)
        conn.execute(
            """INSERT INTO tasks (id, slug, title, destination, owner_harness,
               owner_model, stage, status, source_harness, source_model,
               created_at, updated_at)
               VALUES (42, 'legacy-task', 'Legacy Task', 'any', 'codex', 'gpt-5',
                       'reviewed', 'complete', 'h', 'm',
                       '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO sections (task_id, name, content)
               VALUES (42, 'objective', 'legacy objective')"""
        )
        conn.execute(
            """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
               from_stage, to_stage, note)
               VALUES (42, '2026-01-01T00:00:00Z', 'h', 'm', NULL, 'todo', 'task created')"""
        )
        conn.commit()
        conn.close()

        # Opening via Database() must trigger the additive column migration
        # plus the CHECK-constraint rebuild (ALTER TABLE can't change CHECKs).
        db = Database(db_path)

        columns = {
            row["name"] for row in db._conn().execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "diff" in columns
        assert "verify_cmd" in columns

        table_sql = db._conn().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()["sql"]
        assert "'closed'" in table_sql

        # Ids preserved, and FK-linked child rows survived the rebuild.
        task = db.get_task(42)
        assert task is not None
        assert task.id == 42
        assert task.slug == "legacy-task"
        assert task.stage == "reviewed"
        assert task.sections_dict()["objective"] == "legacy objective"
        assert len(task.transitions) == 1

        # The new reviewed -> closed transition must now work post-migration.
        result = db.transition_task(42, "closed", "human", "human", note="human review: accept")
        assert result.stage == "closed"
        assert result.status == "complete"
