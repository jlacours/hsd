"""MCP server exposing HSD task lifecycle as tools.

Runs on stdio transport. Each MCP-aware harness connects via its own process.
"""

import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult

from hsd.core.db import Database
from hsd.core.models import REQUIRED_SUBMIT_SECTIONS
from hsd.core.rules import (
    validate_transition,
    validate_submit_gate,
    validate_no_self_review,
    validate_model_note,
)
from hsd.core.secret_scan import validate_no_secrets, scan_dict
from hsd.render.markdown import render_task as render_md
from hsd.render.org import render_task as render_org

INSTRUCTIONS = """# HSD Protocol v2

This MCP server manages a shared task board backed by SQLite. Tasks represent
coding handoffs that flow through stages: todo → in-progress → done → reviewed
(with possible human revision).

## Lifecycle
1. **create_task** — any harness can queue a task (todo/queued).
2. **claim_task** — a harness atomically claims it (in-progress).
3. **update_task** — owner updates sections/status while working.
4. **submit_for_review** — gates on required sections, moves to done.
5. **record_review** — no self-review; routes to reviewed/todo/human-revision.
6. **escalate_to_human** — any stage → to-be-revised-by-human.
7. **resolve_human_action** — to-be-revised-by-human → todo.

## Rules
- Claim is atomic: exactly one harness wins.
- No self-review: reviewer harness must differ from owner harness.
- Submit requires: summary_for_review, work_completed, commands_verification, next_actions.
- Secret scan rejects PEM keys, API keys (ghp_, sk-*), AWS keys, JWTs.
- All timestamps are server-stamped UTC.
"""


def _get_tools() -> list[Tool]:
    return [
        Tool(
            name="list_board",
            description="List tasks with optional filters (harness, stage, destination). Returns age + staleness.",
            inputSchema={
                "type": "object",
                "properties": {
                    "harness": {"type": "string", "description": "Filter by owner harness"},
                    "stage": {"type": "string", "description": "Filter by stage (todo, in-progress, done, reviewed, to-be-revised-by-human)"},
                    "destination": {"type": "string", "description": "Filter by destination harness"},
                },
            },
        ),
        Tool(
            name="get_task",
            description="Get full task details including sections, transitions, and reviews.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string", "description": "Task slug or numeric ID"},
                },
                "required": ["slug_or_id"],
            },
        ),
        Tool(
            name="create_task",
            description="Create a new task in todo stage. Server stamps slug and timestamps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "URL-safe unique identifier (kebab-case)"},
                    "title": {"type": "string", "description": "Human-readable title"},
                    "destination": {"type": "string", "description": "Target harness or 'any'"},
                    "sections": {
                        "type": "object",
                        "description": "Section name → markdown content",
                        "additionalProperties": {"type": "string"},
                    },
                    "source_harness": {"type": "string", "description": "Your harness name"},
                    "source_model": {"type": "string", "description": "Your model identifier"},
                    "model_check_note": {"type": "string", "description": "Required if source_model is 'MODEL NOT EXPOSED'"},
                    "author": {"type": "string"},
                    "working_dir": {"type": "string"},
                    "repository": {"type": "string"},
                    "branch_commit": {"type": "string"},
                    "tree_state": {"type": "string"},
                },
                "required": ["slug", "title", "destination", "sections", "source_harness", "source_model"],
            },
        ),
        Tool(
            name="claim_task",
            description="Atomically claim a task (todo → in-progress). Fails if already claimed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "harness": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["slug_or_id", "harness", "model"],
            },
        ),
        Tool(
            name="update_task",
            description="Update sections or status of a task you own.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "section_patches": {
                        "type": "object",
                        "description": "Section name → new content",
                        "additionalProperties": {"type": "string"},
                    },
                    "status": {"type": "string", "enum": ["queued", "in-progress", "blocked", "complete"]},
                },
                "required": ["slug_or_id"],
            },
        ),
        Tool(
            name="submit_for_review",
            description="Submit your task for review (in-progress → done). Requires: summary_for_review, work_completed, commands_verification, next_actions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "sections": {
                        "type": "object",
                        "description": "Required sections for the review gate",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["slug_or_id", "sections"],
            },
        ),
        Tool(
            name="record_review",
            description="Record a review verdict. Rejects self-review (same harness). Routes by verdict: accepted → reviewed, changes-requested → todo, human-revision-required → to-be-revised-by-human.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "reviewer_harness": {"type": "string"},
                    "reviewer_model": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["accepted", "changes-requested", "human-revision-required"]},
                    "findings": {"type": "string"},
                    "disposition": {"type": "string"},
                },
                "required": ["slug_or_id", "reviewer_harness", "reviewer_model", "verdict", "findings", "disposition"],
            },
        ),
        Tool(
            name="escalate_to_human",
            description="Escalate a task to human revision (any stage → to-be-revised-by-human).",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "exact_human_action": {"type": "string"},
                },
                "required": ["slug_or_id", "reason", "exact_human_action"],
            },
        ),
        Tool(
            name="resolve_human_action",
            description="Resolve a human-revision task back to todo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["slug_or_id"],
            },
        ),
        Tool(
            name="export_handoff",
            description="Export a task as markdown or org-mode file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["md", "org"]},
                    "path": {"type": "string", "description": "Output path (optional, returns content if omitted)"},
                },
                "required": ["slug_or_id", "format"],
            },
        ),
        Tool(
            name="board_stats",
            description="Get board statistics: total tasks by stage.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def make_server(db: Database | None = None) -> Server:
    if db is None:
        db = Database()
    server = Server("hsd", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _get_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            return await _handle_call(db, name, arguments)
        except Exception as e:
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(e)}, indent=2),
            )]

    return server


async def _handle_call(
    db: Database, name: str, args: dict
) -> list[TextContent]:
    match name:
        case "list_board":
            tasks = db.list_tasks(
                harness=args.get("harness"),
                stage=args.get("stage"),
                destination=args.get("destination"),
            )
            result = []
            for t in tasks:
                result.append({
                    "id": t.id,
                    "slug": t.slug,
                    "title": t.title,
                    "stage": t.stage,
                    "status": t.status,
                    "destination": t.destination,
                    "owner_harness": t.owner_harness,
                    "owner_model": t.owner_model,
                    "age_hours": round(t.age_hours, 1),
                    "staleness_hours": round(t.staleness_hours, 1),
                    "updated_at": t.updated_at,
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        case "get_task":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")
            return [TextContent(type="text", text=json.dumps(_task_to_dict(task), indent=2))]

        case "create_task":
            # Validate model note
            ok, err = validate_model_note(
                args.get("source_model", ""),
                args.get("model_check_note"),
            )
            if not ok:
                return _error(err)

            # Validate no secrets in sections
            sections = args.get("sections", {})
            for key, content in sections.items():
                ok, err = validate_no_secrets(content)
                if not ok:
                    return _error(f"Secret in section '{key}': {err}")

            # Validate transition legality (Nones → todo)
            task = db.create_task(
                slug=args["slug"],
                title=args["title"],
                destination=args.get("destination", "any"),
                sections=sections,
                source_harness=args["source_harness"],
                source_model=args["source_model"],
                model_check_note=args.get("model_check_note"),
                author=args.get("author"),
                working_dir=args.get("working_dir"),
                repository=args.get("repository"),
                branch_commit=args.get("branch_commit"),
                tree_state=args.get("tree_state"),
            )
            return [TextContent(type="text", text=json.dumps(_task_to_dict(task), indent=2))]

        case "claim_task":
            result = db.claim_task(
                args["slug_or_id"],
                args["harness"],
                args["model"],
            )
            if result is None:
                return _error("Task already claimed or not in todo stage")
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "update_task":
            section_patches = args.get("section_patches")
            if section_patches:
                for key, content in section_patches.items():
                    ok, err = validate_no_secrets(content)
                    if not ok:
                        return _error(f"Secret in section '{key}': {err}")
            result = db.update_task(
                args["slug_or_id"],
                section_patches=section_patches,
                status=args.get("status"),
            )
            if result is None:
                return _error(f"Task not found: {args['slug_or_id']}")
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "submit_for_review":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")

            # Validate transition
            ok, reason = validate_transition(task, "done")
            if not ok:
                return _error(reason)

            # Patch sections first
            sections = args.get("sections", {})
            for key, content in sections.items():
                ok, err = validate_no_secrets(content)
                if not ok:
                    return _error(f"Secret in section '{key}': {err}")

            if sections:
                task = db.update_task(task.slug, section_patches=sections)
                if task is None:
                    return _error("Task not found after update")

            # Check submit gate
            ok, reason = validate_submit_gate(task)
            if not ok:
                return _error(f"Submit gate: {reason}")

            result = db.transition_task(
                task.slug, "done",
                actor_harness=task.owner_harness or "unknown",
                actor_model=task.owner_model or "unknown",
                note="submitted for review",
            )
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "record_review":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")

            # No self-review
            ok, warn = validate_no_self_review(
                task, args["reviewer_harness"], args["reviewer_model"],
            )
            if not ok:
                return _error(warn)

            result, error = db.add_review(
                args["slug_or_id"],
                args["reviewer_harness"],
                args["reviewer_model"],
                args["verdict"],
                args["findings"],
                args["disposition"],
            )
            if error:
                return _error(error)
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "escalate_to_human":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")

            result = db.transition_task(
                task.slug, "to-be-revised-by-human",
                actor_harness=task.owner_harness or "unknown",
                actor_model=task.owner_model or "unknown",
                note=f"Escalated: {args.get('reason', '')}. Human action: {args.get('exact_human_action', '')}",
            )
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "resolve_human_action":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")
            result = db.transition_task(
                task.slug, "todo",
                actor_harness="human",
                actor_model="human",
                note=args.get("note", "Resolved by human"),
            )
            return [TextContent(type="text", text=json.dumps(_task_to_dict(result), indent=2))]

        case "export_handoff":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return _error(f"Task not found: {args['slug_or_id']}")
            fmt = args.get("format", "md")
            if fmt == "md":
                content = render_md(task)
            else:
                content = render_org(task)
            path = args.get("path")
            if path:
                with open(path, "w") as f:
                    f.write(content)
                return [TextContent(type="text", text=json.dumps({"exported_to": path}))]
            return [TextContent(type="text", text=content)]

        case "board_stats":
            stats = db.board_stats()
            return [TextContent(type="text", text=json.dumps(stats, indent=2))]

        case _:
            return _error(f"Unknown tool: {name}")


def _task_to_dict(task: "Task") -> dict:
    return {
        "id": task.id,
        "slug": task.slug,
        "title": task.title,
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
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "sections": {s.name: s.content for s in task.sections},
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


def _error(msg: str) -> list[TextContent]:
    return [TextContent(
        type="text",
        text=json.dumps({"error": msg}, indent=2),
        isError=True,
    )]


def main() -> None:
    """Entry point for hsd-mcp console script."""
    import asyncio

    async def _run() -> None:
        db = Database()
        server = make_server(db)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
