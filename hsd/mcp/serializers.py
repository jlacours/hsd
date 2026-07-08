"""Task serialization for MCP tool responses."""


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
