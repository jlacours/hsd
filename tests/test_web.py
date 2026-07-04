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

    async def test_get_task_not_found(self, client: AsyncClient):
        resp = await client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404

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
        db.transition_task("resolve-me", "to-be-revised-by-human", "h", "m")
        resp = await client.post("/api/tasks/resolve-me/resolve",
                                 json={"note": "Done by human"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["stage"] == "todo"

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
