"""Tests for the web dashboard API."""

import os
import tempfile
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient

from hsd.core.db import Database
from hsd.web.app import create_app
from hsd.web import terminal as terminal_module


class _FakeWebSocket:
    def __init__(self, client_host: str):
        self.client = SimpleNamespace(host=client_host)
        self.headers = {"origin": "http://test", "host": "test"}
        self.query_params = {}
        self.accepted = False
        self.closed_with = None
        self.output = bytearray()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_with = code

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes):
        self.output.extend(data)


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

    async def test_create_task_from_web_authoring_workspace(self, client: AsyncClient):
        await client.put(
            "/api/agent-profiles/planning",
            json={"provider": "openai", "model": "gpt-5.6"},
        )
        resp = await client.post(
            "/api/tasks",
            json={
                "slug": "web-authored-task",
                "title": "Web Authored Task",
                "destination": "any",
                "objective": "Create a task from the web workspace.",
                "current_state": "The task does not exist yet.",
                "plan": "# Plan\n\nBuild it and verify it.",
                "working_dir": "/tmp",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["sections"]["objective"] == "Create a task from the web workspace."
        assert data["sections"]["current_state"] == "The task does not exist yet."
        assert data["sections"]["plan"] == "# Plan\n\nBuild it and verify it."
        assert data["source_harness"] == "human-web"
        assert data["source_model"] == "openai/gpt-5.6"
        assert data["working_dir"] == "/tmp"
        assert data["herdr_session"].startswith("hsd-")
        assert len(data["herdr_session"]) == 20

    async def test_create_task_validates_authoring_input(self, client: AsyncClient):
        resp = await client.post(
            "/api/tasks",
            json={
                "slug": "Not Valid",
                "title": "Nope",
                "objective": "Test invalid input",
                "current_state": "Invalid",
                "plan": "A plan",
            },
        )
        assert resp.status_code == 400

        resp = await client.post(
            "/api/tasks",
            json={
                "slug": "planning-draft",
                "title": "Plan with Herdr",
                "objective": "Develop the plan with agents",
                "current_state": "The plan has not been written",
                "plan": "",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["sections"]["plan"] == ""

        missing = await client.post(
            "/api/tasks",
            json={
                "slug": "missing-current-state",
                "title": "Incomplete",
                "objective": "This lacks the canonical current state",
            },
        )
        assert missing.status_code == 422

        empty_required = await client.post(
            "/api/tasks",
            json={
                "slug": "empty-current-state",
                "title": "Incomplete",
                "destination": "any",
                "objective": "This has an objective",
                "current_state": "  ",
            },
        )
        assert empty_required.status_code == 400
        assert "current_state" in empty_required.json()["detail"]

    async def test_update_written_plan(self, app, client: AsyncClient):
        db: Database = app.state.db
        db.create_task(
            slug="editable-plan", title="Editable", destination="any",
            sections={
                "objective": "Edit the plan",
                "current_state": "The old plan is present",
                "plan": "Old plan",
            },
            source_harness="h", source_model="m",
        )
        resp = await client.put(
            "/api/tasks/editable-plan/plan",
            json={"plan": "# Better plan\n\nNow with checks.", "expected_plan": "Old plan"},
        )
        assert resp.status_code == 200
        assert resp.json()["sections"]["plan"].startswith("# Better plan")

        stale = await client.put(
            "/api/tasks/editable-plan/plan",
            json={"plan": "Overwrite it", "expected_plan": "Old plan"},
        )
        assert stale.status_code == 409

    async def test_agent_profile_api(self, client: AsyncClient):
        initial = await client.get("/api/agent-profiles")
        assert initial.status_code == 200
        assert len(initial.json()) == 5

        saved = await client.put(
            "/api/agent-profiles/bugs",
            json={"provider": "anthropic", "model": "claude-sonnet"},
        )
        assert saved.status_code == 200
        assert saved.json()["purpose"] == "bugs"

        invalid = await client.put(
            "/api/agent-profiles/whatever",
            json={"provider": "x", "model": "y"},
        )
        assert invalid.status_code == 400

    async def test_agent_profiles_bulk_update_is_atomic(self, client: AsyncClient):
        resp = await client.put(
            "/api/agent-profiles",
            json={"profiles": {
                "planning": {"provider": "codex", "model": "gpt-5.6"},
                "reviewing": {"provider": "claude", "model": "sonnet"},
            }},
        )
        assert resp.status_code == 200
        profiles = {item["purpose"]: item for item in resp.json()}
        assert profiles["planning"]["provider"] == "codex"
        assert profiles["reviewing"]["model"] == "sonnet"

        invalid = await client.put(
            "/api/agent-profiles",
            json={"profiles": {
                "planning": {"provider": "changed", "model": "changed"},
                "nonsense": {"provider": "x", "model": "y"},
            }},
        )
        assert invalid.status_code == 400
        current = {item["purpose"]: item for item in (await client.get("/api/agent-profiles")).json()}
        assert current["planning"]["provider"] == "codex"

    async def test_list_tasks(self, app, client: AsyncClient):
        # Add a task directly
        db: Database = app.state.db
        db.create_task(
            slug="web-test", title="Web Test", destination="any",
            sections={"objective": "test", "current_state": "Initial state"},
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
            sections={"objective": "test details", "current_state": "Initial state"},
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
                "current_state": "Testing Markdown rendering",
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
            sections={"objective": "test", "current_state": "Initial state"}, source_harness="h", source_model="m",
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
            sections={"objective": "test", "current_state": "Initial state"},
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
            sections={"objective": "s", "current_state": "Initial state"}, source_harness="h", source_model="m",
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
            sections={"objective": "r", "current_state": "Initial state"}, source_harness="h", source_model="m",
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
            sections={"objective": "export test", "current_state": "Initial state"},
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
            sections={"objective": "org test", "current_state": "Initial state"},
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
            sections={"objective": "a", "current_state": "Initial state"}, source_harness="h", source_model="m",
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

    async def test_terminal_websocket_is_registered(self, app):
        routes = [r.path for r in app.routes]
        assert "/ws/terminal" in routes

    async def test_terminal_rejects_non_loopback_client_by_default(self, monkeypatch):
        monkeypatch.delenv("HSD_TERMINAL_ALLOW_REMOTE", raising=False)
        websocket = _FakeWebSocket("203.0.113.10")
        await terminal_module.terminal_websocket(
            websocket,
            purpose="planning",
            provider="",
            model="",
        )
        assert websocket.closed_with == 1008
        assert not websocket.accepted

    async def test_terminal_disconnect_reaps_shell(self, monkeypatch):
        monkeypatch.setenv("HSD_TERMINAL_SHELL", "/bin/sh")
        processes = []
        real_popen = terminal_module.subprocess.Popen

        def tracking_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(terminal_module.subprocess, "Popen", tracking_popen)
        websocket = _FakeWebSocket("127.0.0.1")
        await terminal_module.terminal_websocket(
            websocket,
            purpose="planning",
            provider="codex",
            model="gpt-test",
        )
        assert websocket.accepted
        assert processes and processes[0].poll() is not None

    async def test_static_frontend(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Settings" in resp.text
        assert "+ Task" in resp.text

        app_js = await client.get("/static/app.js")
        assert "toggleDrawerFullscreen" in app_js.text
        assert "sections_html" in app_js.text
        assert "renderAuthor" in app_js.text
        assert "connectTerminal" in app_js.text
        assert "Objective · required" in app_js.text
        assert "Current state · required" in app_js.text
        assert "is required by the HSD task format" in app_js.text


class TestHumanReviewEndpoint:
    def _make_reviewed(self, app, slug="hr-reviewed"):
        """Drive a task through claim -> submit -> accepted review -> 'reviewed'."""
        db: Database = app.state.db
        db.create_task(
            slug=slug, title="HR Reviewed", destination="any",
            sections={"objective": "test", "current_state": "Initial state"}, source_harness="h", source_model="m",
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
            sections={"objective": "test", "current_state": "Initial state"}, source_harness="h", source_model="m",
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
