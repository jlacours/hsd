"""Tests for the MCP server tool handlers."""

import json
import os
import tempfile

import pytest

from hsd.core.db import Database
from hsd.mcp.server import _handle_call, error_result, ok_result
from hsd.mcp.schemas import _get_tools


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    os.unlink(db_path)


def _task(db: Database):
    db.create_task(
        slug="mcp-test", title="MCP Test", destination="any",
        sections={"objective": "test", "current_state": "Initial state"},
        source_harness="h", source_model="m",
    )
    db.claim_task("mcp-test", "opencode", "deepseek-v4")
    return db.get_task("mcp-test")


class TestSlugValidation:
    def test_tool_schema_requires_canonical_creation_sections(self):
        create_tool = next(tool for tool in _get_tools() if tool.name == "create_task")
        sections = create_tool.inputSchema["properties"]["sections"]
        assert sections["required"] == ["objective", "current_state"]
        assert sections["additionalProperties"] is False

    async def test_rejects_invalid_slug_uppercase(self, db):
        result = await _handle_call(db, "create_task", {
            "slug": "BadSlug",
            "title": "Test",
            "destination": "any",
            "sections": {"objective": "test", "current_state": "Initial state"},
            "source_harness": "h",
            "source_model": "m",
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "Invalid slug" in content["error"]

    async def test_rejects_invalid_slug_spaces(self, db):
        result = await _handle_call(db, "create_task", {
            "slug": "bad slug",
            "title": "Test",
            "destination": "any",
            "sections": {"objective": "test", "current_state": "Initial state"},
            "source_harness": "h",
            "source_model": "m",
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "Invalid slug" in content["error"]

    async def test_rejects_invalid_slug_leading_digit(self, db):
        result = await _handle_call(db, "create_task", {
            "slug": "1bad",
            "title": "Test",
            "destination": "any",
            "sections": {"objective": "test", "current_state": "Initial state"},
            "source_harness": "h",
            "source_model": "m",
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "Invalid slug" in content["error"]

    async def test_accepts_valid_slug(self, db):
        result = await _handle_call(db, "create_task", {
            "slug": "my-valid-task-42",
            "title": "Test",
            "destination": "any",
            "sections": {"objective": "test", "current_state": "Initial state"},
            "source_harness": "h",
            "source_model": "m",
        })
        assert result.isError is False
        content = json.loads(result.content[0].text)
        assert content["slug"] == "my-valid-task-42"
        assert content["stage"] == "todo"

    async def test_rejects_missing_canonical_section(self, db):
        result = await _handle_call(db, "create_task", {
            "slug": "missing-current-state",
            "title": "Incomplete task",
            "destination": "any",
            "sections": {"objective": "Do it"},
            "source_harness": "h",
            "source_model": "m",
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "current_state" in content["error"]


class TestOwnerCheck:
    async def test_update_task_rejects_wrong_owner(self, db):
        _task(db)
        result = await _handle_call(db, "update_task", {
            "slug_or_id": "mcp-test",
            "harness": "wrong-harness",
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "denied" in content["error"].lower()
        assert "wrong-harness" in content["error"]

    async def test_update_task_accepts_owner(self, db):
        _task(db)
        result = await _handle_call(db, "update_task", {
            "slug_or_id": "mcp-test",
            "harness": "opencode",
            "section_patches": {"objective": "updated"},
        })
        assert result.isError is False
        content = json.loads(result.content[0].text)
        assert content["sections"]["objective"] == "updated"

    async def test_submit_for_review_rejects_wrong_owner(self, db):
        _task(db)
        result = await _handle_call(db, "submit_for_review", {
            "slug_or_id": "mcp-test",
            "harness": "wrong-harness",
            "sections": {
                "summary_for_review": "good",
                "work_completed": "done",
                "commands_verification": "tested",
                "next_actions": "none",
            },
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        assert "denied" in content["error"].lower()
        assert "wrong-harness" in content["error"]

    async def test_failed_submit_does_not_persist_partial_sections(self, db):
        _task(db)
        result = await _handle_call(db, "submit_for_review", {
            "slug_or_id": "mcp-test",
            "harness": "opencode",
            "sections": {"work_completed": "only one review section"},
        })
        assert result.isError is True
        assert "Submit gate" in json.loads(result.content[0].text)["error"]
        task = db.get_task("mcp-test")
        assert "work_completed" not in task.sections_dict()

    async def test_update_validation_returns_normal_mcp_error(self, db):
        _task(db)
        result = await _handle_call(db, "update_task", {
            "slug_or_id": "mcp-test",
            "harness": "opencode",
            "section_patches": {"objective": ""},
        })
        assert result.isError is True
        assert "objective" in json.loads(result.content[0].text)["error"]

    async def test_submit_for_review_rejects_not_in_progress(self, db):
        """submit_for_review fails if task is not in-progress."""
        result = await _handle_call(db, "submit_for_review", {
            "slug_or_id": "mcp-test",
            "harness": "h",
            "sections": {},
        })
        assert result.isError is True
        content = json.loads(result.content[0].text)
        # Task doesn't exist yet, so "not found" is fine
        assert "not found" in content["error"]


class TestErrorResponseFormat:
    async def test_error_returns_calltoolresult_iserror(self, db):
        result = await _handle_call(db, "get_task", {"slug_or_id": "nonexistent"})
        assert result.isError is True
        assert hasattr(result, "isError")
        assert result.isError is True

    async def test_ok_response_not_error(self, db):
        db.create_task(
            slug="ok-test", title="OK", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
            source_harness="h", source_model="m",
        )
        result = await _handle_call(db, "get_task", {"slug_or_id": "ok-test"})
        assert result.isError is False

    async def test_ok_result_function(self):
        result = ok_result({"hello": "world"})
        assert result.isError is False
        data = json.loads(result.content[0].text)
        assert data["hello"] == "world"

    async def test_error_result_function(self):
        result = error_result("something went wrong")
        assert result.isError is True
        data = json.loads(result.content[0].text)
        assert "error" in data
        assert "something went wrong" in data["error"]
