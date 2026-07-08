"""Tool call dispatch and task serialization for the MCP server."""

import re

from mcp.types import CallToolResult

from hsd.core.db import Database
from hsd.core.rules import (
    validate_transition,
    validate_submit_gate,
    validate_no_self_review,
    validate_model_note,
)
from hsd.core.secret_scan import validate_no_secrets
from hsd.render.markdown import render_task as render_md
from hsd.render.org import render_task as render_org

from hsd.mcp.results import ok_result, error_result
from hsd.mcp.serializers import _task_to_dict


async def _handle_call(
    db: Database, name: str, args: dict
) -> CallToolResult:
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
            return ok_result(result)

        case "get_task":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")
            return ok_result(_task_to_dict(task))

        case "create_task":
            # Validate slug is kebab-case
            slug = args["slug"]
            if not re.match(r'^[a-z][a-z0-9-]*$', slug):
                return error_result(
                    f"Invalid slug: {slug!r}. Slug must be kebab-case "
                    f"(lowercase letters, digits, hyphens only)."
                )
            # Validate model note
            ok, err = validate_model_note(
                args.get("source_model", ""),
                args.get("model_check_note"),
            )
            if not ok:
                return error_result(err)

            # Validate no secrets in sections
            sections = args.get("sections", {})
            for key, content in sections.items():
                ok, err = validate_no_secrets(content)
                if not ok:
                    return error_result(f"Secret in section '{key}': {err}")

            task = db.create_task(
                slug=slug,
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
            return ok_result(_task_to_dict(task))

        case "claim_task":
            result = db.claim_task(
                args["slug_or_id"],
                args["harness"],
                args["model"],
            )
            if result is None:
                return error_result("Task already claimed or not in todo stage")
            return ok_result(_task_to_dict(result))

        case "update_task":
            # Owner check
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")
            harness = args["harness"]
            if task.owner_harness and harness.lower() != task.owner_harness.lower():
                return error_result(
                    f"update_task denied: harness '{harness}' does not match "
                    f"owner '{task.owner_harness}'"
                )
            if task.stage != "in-progress":
                return error_result(
                    f"update_task denied: task is in '{task.stage}', "
                    f"only 'in-progress' tasks can be updated"
                )

            section_patches = args.get("section_patches")
            if section_patches:
                for key, content in section_patches.items():
                    ok, err = validate_no_secrets(content)
                    if not ok:
                        return error_result(f"Secret in section '{key}': {err}")
            result = db.update_task(
                args["slug_or_id"],
                section_patches=section_patches,
                status=args.get("status"),
            )
            if result is None:
                return error_result(f"Task not found: {args['slug_or_id']}")
            return ok_result(_task_to_dict(result))

        case "submit_for_review":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")

            # Owner check
            harness = args.get("harness")
            if task.owner_harness and harness.lower() != task.owner_harness.lower():
                return error_result(
                    f"submit_for_review denied: harness '{harness}' does not match "
                    f"owner '{task.owner_harness}'"
                )

            # Validate transition
            ok, reason = validate_transition(task, "done")
            if not ok:
                return error_result(reason)

            # Patch sections first
            sections = args.get("sections", {})
            for key, content in sections.items():
                ok, err = validate_no_secrets(content)
                if not ok:
                    return error_result(f"Secret in section '{key}': {err}")

            if sections:
                task = db.update_task(task.slug, section_patches=sections)
                if task is None:
                    return error_result("Task not found after update")

            # Check submit gate
            ok, reason = validate_submit_gate(task)
            if not ok:
                return error_result(f"Submit gate: {reason}")

            result = db.transition_task(
                task.slug, "done",
                actor_harness=harness or task.owner_harness or "unknown",
                actor_model=args.get("model") or task.owner_model or "unknown",
                note="submitted for review",
                diff=args.get("diff"),
                verify_cmd=args.get("verify_cmd"),
            )
            return ok_result(_task_to_dict(result))

        case "record_review":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")

            # No self-review
            ok, warn = validate_no_self_review(
                task, args["reviewer_harness"], args["reviewer_model"],
            )
            if not ok:
                return error_result(warn)

            result, error = db.add_review(
                args["slug_or_id"],
                args["reviewer_harness"],
                args["reviewer_model"],
                args["verdict"],
                args["findings"],
                args["disposition"],
            )
            if error:
                return error_result(error)
            return ok_result(_task_to_dict(result))

        case "escalate_to_human":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")

            result = db.transition_task(
                task.slug, "to-be-revised-by-human",
                actor_harness=task.owner_harness or "unknown",
                actor_model=task.owner_model or "unknown",
                note=f"Escalated: {args.get('reason', '')}. Human action: {args.get('exact_human_action', '')}",
            )
            return ok_result(_task_to_dict(result))

        case "resolve_human_action":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")
            result = db.transition_task(
                task.slug, "todo",
                actor_harness="human",
                actor_model="human",
                note=args.get("note", "Resolved by human"),
            )
            return ok_result(_task_to_dict(result))

        case "export_handoff":
            task = db.get_task(args["slug_or_id"])
            if task is None:
                return error_result(f"Task not found: {args['slug_or_id']}")
            fmt = args.get("format", "md")
            if fmt == "md":
                content = render_md(task)
            else:
                content = render_org(task)
            path = args.get("path")
            if path:
                with open(path, "w") as f:
                    f.write(content)
                return ok_result({"exported_to": path})
            return ok_result(content)

        case "board_stats":
            stats = db.board_stats()
            return ok_result(stats)

        case _:
            return error_result(f"Unknown tool: {name}")
