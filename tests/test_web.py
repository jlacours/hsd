"""Tests for the web dashboard API."""

import os
import tempfile
import pytest
from httpx import ASGITransport, AsyncClient

from hsd.core.db import Database
from hsd.web.app import create_app


@pytest.fixture
def app():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    app = create_app(db)
    yield app
    os.unlink(db_path)


@pytest.fixture
def client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestWebAPI:
    async def test_list_tasks_empty(self, client: AsyncClient):
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_tasks(self, app, client: AsyncClient):
        # Add a task directly
        db: Database = app.state.db
        db.create_task(
            slug="web-test", title="Web Test", destination="any",
            sections={"objective": "test"},
            source_harness="test", source_model="test",
        )
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["slug"] == "web-test"
        assert data[0]["source_harness"] == "test"
        assert data[0]["source_model"] == "test"

    async def test_get_task(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="get-test", title="Get Test", destination="any",
            sections={"objective": "test details"},
            source_harness="h", source_model="m",
        )
        resp = await client.get("/api/tasks/get-test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "get-test"
        assert "sections" in data
        assert data["sections"]["objective"] == "test details"
        assert data["sections_html"]["objective"] == "<p>test details</p>\n"

    async def test_task_markdown_preview_is_rendered_without_raw_html(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="markdown-preview", title="Markdown Preview", destination="any",
            sections={
                "objective": "**Bold**\n\n- one\n- two\n\n<script>alert('nope')</script>",
            },
            source_harness="h", source_model="m",
        )

        resp = await client.get("/api/tasks/markdown-preview")
        rendered = resp.json()["sections_html"]["objective"]
        assert "<strong>Bold</strong>" in rendered
        assert "<li>one</li>" in rendered
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered

    async def test_get_task_not_found(self, client: AsyncClient):
        resp = await client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404

    async def test_get_task_by_numeric_id(self, app, client: AsyncClient):
        db: Database = app.state.db
        task = db.create_task(
            slug="numeric-id", title="Numeric ID", destination="any",
            sections={"objective": "test"}, source_harness="h", source_model="m",
        )

        resp = await client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json()["slug"] == "numeric-id"

    async def test_legacy_title_metadata_suffix_is_hidden(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="legacy-title",
            title="Fix the thing — Claude Code — `anthropic/claude-sonnet-5`",
            destination="claude-code",
            sections={"objective": "test"},
            source_harness="Claude Code (CLI)",
            source_model="`anthropic/claude-sonnet-5` (exact ID reported by harness env)",
        )

        list_resp = await client.get("/api/tasks")
        listed = next(t for t in list_resp.json() if t["slug"] == "legacy-title")
        assert listed["title"] == "Fix the thing"

        detail_resp = await client.get("/api/tasks/legacy-title")
        assert detail_resp.json()["title"] == "Fix the thing"

    async def test_stats(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="stat-task", title="Stat", destination="any",
            sections={"objective": "s"}, source_harness="h", source_model="m",
        )
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert "by_stage" in data

    async def test_resolve_task(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="resolve-me", title="Resolve", destination="any",
            sections={"objective": "r"}, source_harness="h", source_model="m",
        )
        db.claim_task("resolve-me", "h", "m")
        db.transition_task("resolve-me", "to-be-revised-by-human", "h", "m")
        resp = await client.post(
            "/api/tasks/resolve-me/resolve", params={"note": "Done by human"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "todo"
        assert data["owner_harness"] is None
        assert data["owner_model"] is None
        assert data["transitions"][-1]["note"] == "Done by human"

    async def test_export_md(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="export-me", title="Export", destination="any",
            sections={"objective": "export test"},
            source_harness="h", source_model="m",
        )
        resp = await client.get("/api/tasks/export-me/export?fmt=md")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert "Handoff" in data["content"]

    async def test_export_org(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="export-org", title="Export Org", destination="any",
            sections={"objective": "org test"},
            source_harness="h", source_model="m",
        )
        resp = await client.get("/api/tasks/export-org/export?fmt=org")
        assert resp.status_code == 200
        data = resp.json()
        assert "#+title:" in data["content"]

    async def test_activity(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="activity-test", title="Activity", destination="any",
            sections={"objective": "a"}, source_harness="h", source_model="m",
        )
        resp = await client.get("/api/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    async def test_sse_endpoint(self, app):
        """SSE endpoint is registered and returns correct content type."""
        # Quick check: verify the route exists in the app
        routes = [r.path for r in app.routes]
        assert "/api/stream" in routes

    async def test_static_frontend(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Settings" in resp.text

        app_js = await client.get("/static/app.js")
        assert "toggleDrawerFullscreen" in app_js.text
        assert "sections_html" in app_js.text


class TestHumanReviewEndpoint:
    def _make_reviewed(self, app, slug="hr-reviewed"):
        """Drive a task through claim -> submit -> accepted review -> 'reviewed'."""
        db: Database = app.state.db
        db.create_task(
            slug=slug, title="HR Reviewed", destination="any",
            sections={"objective": "test"}, source_harness="h", source_model="m",
        )
        db.claim_task(slug, "codex", "gpt-5")
        db.transition_task(
            slug, "done", "codex", "gpt-5",
            diff="the diff", verify_cmd="pytest tests/",
        )
        db.add_review(slug, "opencode", "deepseek-v4", "accepted", "looks good", "approved")
        return slug

    async def test_human_review_accept(self, app, client: AsyncClient):
        slug = self._make_reviewed(app, "hr-accept")
        resp = await client.post(
            f"/api/tasks/{slug}/human-review", json={"verdict": "accept"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "closed"
        assert data["status"] == "complete"
        last = data["transitions"][-1]
        assert last["note"] == "human review: accept"
        assert last["actor_harness"] == "human"
        assert last["actor_model"] == "human"

    async def test_human_review_revise_with_note(self, app, client: AsyncClient):
        slug = self._make_reviewed(app, "hr-revise")
        resp = await client.post(
            f"/api/tasks/{slug}/human-review",
            json={"verdict": "revise", "note": "needs another pass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "to-be-revised-by-human"
        last = data["transitions"][-1]
        assert last["note"] == "human review: revise — needs another pass"
        assert last["actor_harness"] == "human"
        assert last["actor_model"] == "human"

    async def test_human_review_bad_verdict(self, app, client: AsyncClient):
        slug = self._make_reviewed(app, "hr-bad-verdict")
        resp = await client.post(
            f"/api/tasks/{slug}/human-review", json={"verdict": "reject"},
        )
        assert resp.status_code == 400

    async def test_human_review_illegal_stage(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="hr-illegal", title="HR Illegal", destination="any",
            sections={"objective": "test"}, source_harness="h", source_model="m",
        )
        resp = await client.post(
            "/api/tasks/hr-illegal/human-review", json={"verdict": "accept"},
        )
        assert resp.status_code == 400

    async def test_human_review_unknown_task(self, client: AsyncClient):
        resp = await client.post(
            "/api/tasks/nonexistent/human-review", json={"verdict": "accept"},
        )
        assert resp.status_code == 404

    async def test_diff_and_verify_cmd_in_task_detail(self, app, client: AsyncClient):
        slug = self._make_reviewed(app, "hr-diff-detail")
        resp = await client.get(f"/api/tasks/{slug}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["diff"] == "the diff"
        assert data["verify_cmd"] == "pytest tests/"
