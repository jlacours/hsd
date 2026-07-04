"""Tests for the core database layer and domain rules."""

import concurrent.futures
import threading

import pytest

from hsd.core.db import Database
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
            sections={"objective": "Do the thing"},
            source_harness="opencode",
            source_model="deepseek-v4",
        )
        assert task.id is not None
        assert task.slug == "my-task"
        assert task.title == "My Task"
        assert task.stage == "todo"
        assert task.status == "queued"
        assert task.destination == "opencode"
        assert task.sections[0].name == "objective"

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
            sections={"objective": "test"},
            source_harness="test",
            source_model="test",
        )
        assert task.created_at.endswith("Z")
        assert task.updated_at.endswith("Z")
        assert task.created_at == task.updated_at

    def test_duplicate_slug_raises(self, db: Database):
        db.create_task(
            slug="dup", title="First", destination="any",
            sections={"objective": "a"}, source_harness="h", source_model="m",
        )
        with pytest.raises(Exception):
            db.create_task(
                slug="dup", title="Second", destination="any",
                sections={"objective": "b"}, source_harness="h", source_model="m",
            )

    def test_invalid_section_name_raises(self, db: Database):
        with pytest.raises(ValueError, match="invalid section name"):
            db.create_task(
                slug="bad-section", title="Bad", destination="any",
                sections={"nonexistent": "content"},
                source_harness="h", source_model="m",
            )

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
            sections={"objective": "race test"},
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
            source_harness="h", source_model="m",
        )
        db.claim_task(task.slug, "codex", "gpt-5")
        task = db.get_task(task.slug)
        ok, warn = validate_no_self_review(task, "opencode", "deepseek-v4")
        assert ok
        assert not warn


class TestSecretScan:
    def test_rejects_pem_key(self):
        ok, err = validate_no_secrets("-----BEGIN RSA PRIVATE KEY-----\nABCD\n-----END RSA PRIVATE KEY-----")
        assert not ok
        assert "PEM" in err

    def test_rejects_github_pat(self):
        ok, err = validate_no_secrets("use ghp_abcdefghijklmnopqrstuvwxyz1234567890")
        assert not ok
        assert "ghp_" in err

    def test_rejects_aws_key(self):
        ok, err = validate_no_secrets("AKIA0123456789ABCDEF")
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
        db.create_task(slug="a", title="A", destination="any", sections={"objective": "a"}, source_harness="h", source_model="m")
        db.create_task(slug="b", title="B", destination="opencode", sections={"objective": "b"}, source_harness="h", source_model="m")
        tasks = db.list_tasks()
        assert len(tasks) >= 2

    def test_filter_by_harness(self, db):
        db.create_task(slug="c1", title="C1", destination="any", sections={"objective": "c1"}, source_harness="opencode", source_model="m")
        db.create_task(slug="c2", title="C2", destination="any", sections={"objective": "c2"}, source_harness="codex", source_model="m")
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
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
            sections={"objective": "test"},
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
        assert "not awaiting review" in error
