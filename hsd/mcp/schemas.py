"""Tool schema definitions and server instructions for the MCP server."""

from mcp.types import Tool

from hsd.core.task_format import CANONICAL_SECTION_NAMES

INSTRUCTIONS = """# HSD Protocol v2

This MCP server manages a shared task board backed by SQLite. Tasks represent
coding handoffs that flow through stages: todo -> in-progress -> done -> reviewed
(with possible human revision).

## Lifecycle
1. **create_task** -- any harness can queue a task (todo/queued).
2. **claim_task** -- a harness atomically claims it (in-progress).
3. **update_task** -- owner updates sections/status while working.
4. **submit_for_review** -- gates on required sections, moves to done. Include diff and verify_cmd for human reviewers.
5. **record_review** -- no self-review; routes to reviewed/todo/human-revision.
6. reviewed is NOT terminal: a HUMAN (via web dashboard or CLI) either accepts (reviewed -> closed) or requests revision (reviewed -> to-be-revised-by-human).
7. **escalate_to_human** -- any stage -> to-be-revised-by-human.
8. **resolve_human_action** -- to-be-revised-by-human -> todo.

## Rules
- Creation requires a kebab-case slug plus non-empty objective and current_state sections.
- plan is optional at creation so planning can continue after the task is persisted.
- Claim is atomic: exactly one harness wins.
- No self-review: reviewer harness must differ from owner harness.
- Owner-only: update_task and submit_for_review require caller identity.
- Submit requires: summary_for_review, work_completed, commands_verification, next_actions.
- Models cannot close tasks: only humans via the web dashboard or CLI can accept or reject reviewed tasks.
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
                    "slug": {"type": "string", "description": "URL-safe unique identifier (kebab-case, e.g. 'my-task-name')"},
                    "title": {
                        "type": "string",
                        "description": "Concise task name only; do not append harness or model metadata",
                    },
                    "destination": {"type": "string", "description": "Target harness or 'any'"},
                    "sections": {
                        "type": "object",
                        "description": "Canonical HSD sections. Objective and current_state must be non-empty; plan may initially be empty.",
                        "properties": {
                            name: {"type": "string"}
                            for name in CANONICAL_SECTION_NAMES
                        },
                        "required": ["objective", "current_state"],
                        "additionalProperties": False,
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
            description="Atomically claim a task (todo -> in-progress). Fails if already claimed.",
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
            description="Update sections or status of a task you own. Requires harness+model to verify ownership.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "harness": {"type": "string", "description": "Your harness name (must match owner)"},
                    "model": {"type": "string", "description": "Your model identifier"},
                    "section_patches": {
                        "type": "object",
                        "description": "Section name -> new content",
                        "additionalProperties": {"type": "string"},
                    },
                    "status": {"type": "string", "enum": ["queued", "in-progress", "blocked", "complete"]},
                },
                "required": ["slug_or_id", "harness"],
            },
        ),
        Tool(
            name="submit_for_review",
            description="Submit your task for review (in-progress -> done). Requires caller harness+model for ownership check and: summary_for_review, work_completed, commands_verification, next_actions. Include diff and verify_cmd so humans have concrete context to review.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_id": {"type": "string"},
                    "harness": {"type": "string", "description": "Your harness name (must match owner)"},
                    "model": {"type": "string", "description": "Your model identifier"},
                    "sections": {
                        "type": "object",
                        "description": "Required sections for the review gate",
                        "additionalProperties": {"type": "string"},
                    },
                    "diff": {"type": "string", "description": "Unified git diff of the change (optional)"},
                    "verify_cmd": {"type": "string", "description": "Command a human can run to verify the change (optional)"},
                },
                "required": ["slug_or_id", "harness", "sections"],
            },
        ),
        Tool(
            name="record_review",
            description="Record a review verdict. Rejects self-review (same harness). Routes by verdict: accepted -> reviewed, changes-requested -> todo, human-revision-required -> to-be-revised-by-human.",
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
            description="Escalate a task to human revision (any stage -> to-be-revised-by-human).",
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
