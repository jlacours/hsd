"""FastAPI web dashboard for HSD."""

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel

from hsd.core.db import Database
from hsd.core.rules import validate_no_self_review
from hsd.core.secret_scan import validate_no_secrets
from hsd.render.markdown import render_task as render_md
from hsd.render.org import render_task as render_org

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})

HUMAN_REVIEW_VERDICTS = {"accept": "closed", "revise": "to-be-revised-by-human"}


class HumanReviewRequest(BaseModel):
    verdict: str
    note: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on app startup."""
    poll_task = asyncio.create_task(_poll_data_version(app))
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass


def create_app(db: Database | None = None) -> FastAPI:
    if db is None:
        db_path = os.environ.get("HSD_DB_PATH")
        db = Database(db_path)

    app = FastAPI(
        title="HSD Dashboard",
        lifespan=lifespan,
    )

    app.state.db = db
    app.state._last_data_version = 0
    app.state._sse_clients: list[asyncio.Queue] = []

    # REST API

    @app.get("/api/tasks")
    async def list_tasks(
        harness: str | None = None,
        stage: str | None = None,
        destination: str | None = None,
    ):
        tasks = db.list_tasks(harness=harness, stage=stage, destination=destination)
        return [_task_summary(t) for t in tasks]

    @app.get("/api/tasks/{slug_or_id}")
    async def get_task(slug_or_id: str):
        task = db.get_task(slug_or_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return _task_detail(task)

    @app.get("/api/stats")
    async def get_stats():
        return db.board_stats()

    @app.get("/api/activity")
    async def get_activity(limit: int = 50):
        """Return recent transitions across all tasks."""
        tasks = db.list_tasks()
        all_transitions = []
        for t in tasks:
            task = db.get_task(t.slug)
            if task:
                for tr in task.transitions:
                    all_transitions.append({
                        "task_slug": task.slug,
                        "task_title": _display_title(task),
                        "at": tr.at,
                        "actor_harness": tr.actor_harness,
                        "actor_model": tr.actor_model,
                        "from_stage": tr.from_stage,
                        "to_stage": tr.to_stage,
                        "note": tr.note,
                    })
        all_transitions.sort(key=lambda x: x["at"], reverse=True)
        return all_transitions[:limit]

    @app.post("/api/tasks/{slug_or_id}/resolve")
    async def resolve_task(slug_or_id: str, note: str = "Resolved by human"):
        task = db.get_task(slug_or_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        result = db.transition_task(
            slug_or_id, "todo",
            actor_harness="human",
            actor_model="human",
            note=note,
        )
        _notify_clients(app)
        return _task_detail(result)

    @app.post("/api/tasks/{slug_or_id}/human-review")
    async def human_review_task(slug_or_id: str, body: HumanReviewRequest):
        task = db.get_task(slug_or_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        to_stage = HUMAN_REVIEW_VERDICTS.get(body.verdict)
        if to_stage is None:
            raise HTTPException(status_code=400, detail=f"Invalid verdict: {body.verdict}")
        note = f"human review: {body.verdict}"
        if body.note:
            note += f" — {body.note}"
        try:
            result = db.transition_task(
                slug_or_id, to_stage,
                actor_harness="human",
                actor_model="human",
                note=note,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _notify_clients(app)
        return _task_detail(result)

    @app.get("/api/tasks/{slug_or_id}/export")
    async def export_task(slug_or_id: str, fmt: str = "md"):
        task = db.get_task(slug_or_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if fmt == "md":
            content = render_md(task)
            media_type = "text/markdown"
        else:
            content = render_org(task)
            media_type = "text/plain"
        return JSONResponse(
            content={"content": content, "media_type": media_type},
        )

    @app.get("/api/stream")
    async def sse_stream(request: Request):
        """SSE endpoint: pushes 'refresh' events when data_version changes."""
        queue: asyncio.Queue = asyncio.Queue()
        app.state._sse_clients.append(queue)

        async def event_generator():
            try:
                while True:
                    await queue.get()
                    # Send a minimal JSON event — client re-fetches
                    yield f"data: {{\"event\": \"refresh\"}}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                app.state._sse_clients.remove(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Static frontend

    # Serve index.html for the root
    @app.get("/")
    async def serve_index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    # Mount static files
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def _poll_data_version(app: FastAPI) -> None:
    """Poll PRAGMA data_version every second and notify SSE clients on change."""
    db: Database = app.state.db
    while True:
        try:
            dv = db.data_version()
            if dv != app.state._last_data_version:
                app.state._last_data_version = dv
                _notify_clients(app)
        except Exception:
            pass  # keep polling
        await asyncio.sleep(1)


def _notify_clients(app: FastAPI) -> None:
    """Push a refresh event to all connected SSE clients."""
    for queue in app.state._sse_clients:
        try:
            queue.put_nowait("refresh")
        except asyncio.QueueFull:
            pass


def _task_summary(task: "Task") -> dict:
    return {
        "id": task.id,
        "slug": task.slug,
        "title": _display_title(task),
        "stage": task.stage,
        "status": task.status,
        "destination": task.destination,
        "owner_harness": task.owner_harness,
        "owner_model": task.owner_model,
        "source_harness": task.source_harness,
        "source_model": task.source_model,
        "age_hours": round(task.age_hours, 1),
        "staleness_hours": round(task.staleness_hours, 1),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_detail(task: "Task") -> dict:
    return {
        "id": task.id,
        "slug": task.slug,
        "title": _display_title(task),
        "destination": task.destination,
        "owner_harness": task.owner_harness,
        "owner_model": task.owner_model,
        "stage": task.stage,
        "status": task.status,
        "source_harness": task.source_harness,
        "source_model": task.source_model,
        "model_check_note": task.model_check_note,
        "author": task.author,
        "working_dir": task.working_dir,
        "repository": task.repository,
        "branch_commit": task.branch_commit,
        "tree_state": task.tree_state,
        "diff": task.diff,
        "verify_cmd": task.verify_cmd,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "sections": {s.name: s.content for s in task.sections},
        "sections_html": {s.name: MARKDOWN.render(s.content) for s in task.sections},
        "transitions": [
            {
                "id": t.id,
                "at": t.at,
                "actor_harness": t.actor_harness,
                "actor_model": t.actor_model,
                "from_stage": t.from_stage,
                "to_stage": t.to_stage,
                "note": t.note,
            }
            for t in task.transitions
        ],
        "reviews": [
            {
                "id": r.id,
                "at": r.at,
                "reviewer_harness": r.reviewer_harness,
                "reviewer_model": r.reviewer_model,
                "verdict": r.verdict,
                "findings": r.findings,
                "disposition": r.disposition,
            }
            for r in task.reviews
        ],
    }


def _display_title(task: "Task") -> str:
    """Remove legacy title suffixes that duplicate stored source metadata."""
    parts = task.title.rsplit(" — ", 2)
    if len(parts) != 3:
        return task.title

    title, harness, model = parts
    if (
        _normalize_harness(harness) == _normalize_harness(task.source_harness)
        and _normalize_model(model) == _normalize_model(task.source_model)
    ):
        return title
    return task.title


def _normalize_harness(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value.lower())
    value = re.sub(r"\bcli\b", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def _normalize_model(value: str) -> str:
    value = value.strip()
    quoted = re.match(r"^`([^`]+)`", value)
    if quoted:
        value = quoted.group(1)
    else:
        value = value.split(" (", 1)[0].strip().strip("`")
    return value.lower()


app = create_app()


def main() -> None:
    import uvicorn
    host = os.environ.get("HSD_HOST", "127.0.0.1")
    port = int(os.environ.get("HSD_PORT", "8737"))
    uvicorn.run("hsd.web.app:app", host=host, port=port, reload=False)
