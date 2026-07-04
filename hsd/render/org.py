"""Org-mode handoff renderer."""

from hsd.core.models import Task, Section

SECTION_TITLES: dict[str, str] = {
    "objective": "Objective",
    "current_state": "Current State",
    "summary_for_review": "Summary for Review",
    "work_completed": "Work Completed",
    "files_changed": "Files Changed",
    "commands_verification": "Commands and Verification",
    "decisions_assumptions": "Decisions and Assumptions",
    "blockers_risks": "Blockers and Risks",
    "warnings": "Warnings",
    "next_actions": "Next Actions",
    "artifacts": "Artifacts and References",
    "continuation_prompt": "Continuation Prompt",
    "raw": "Raw Content",
}


def render_task(task: Task) -> str:
    """Render a Task as a complete org-mode handoff document."""
    lines: list[str] = []
    lines.append(f"#+title: Handoff: {task.title}")
    lines.append(f"#+date: {task.created_at}")
    lines.append("")

    # Metadata
    lines.append("* Metadata")
    lines.append("")
    col_width = 20
    lines.append(f"| {'Field':<{col_width}} | {'Value':<60} |")
    lines.append(f"| {'-'*col_width} | {'-'*60} |")
    org_row("Source harness", task.source_harness, col_width, lines)
    org_row("Model", task.source_model, col_width, lines)
    org_row("Destination", task.destination, col_width, lines)
    org_row("Stage", task.stage, col_width, lines)
    org_row("Status", task.status, col_width, lines)
    if task.owner_harness:
        org_row("Owner harness", task.owner_harness, col_width, lines)
    if task.owner_model:
        org_row("Owner model", task.owner_model, col_width, lines)
    if task.working_dir:
        org_row("Working directory", task.working_dir, col_width, lines)
    if task.branch_commit:
        org_row("Branch/commit", task.branch_commit, col_width, lines)
    if task.tree_state:
        org_row("Tree state", task.tree_state, col_width, lines)
    org_row("Created", task.created_at, col_width, lines)
    org_row("Updated", task.updated_at, col_width, lines)
    lines.append("")

    # Sections
    for section in task.sections:
        title = SECTION_TITLES.get(section.name, section.name.replace("_", " ").title())
        lines.append(f"* {title}")
        lines.append("")
        if section.name == "continuation_prompt":
            lines.append("#+begin_quote")
            lines.append(section.content)
            lines.append("#+end_quote")
        else:
            lines.append(section.content)
        lines.append("")

    # Transitions
    if task.transitions:
        lines.append("* Timeline")
        lines.append("")
        lines.append(f"| {'When':<30} | {'Actor':<30} | {'From':<20} | {'To':<25} | {'Note':<30} |")
        lines.append(f"| {'-'*30} | {'-'*30} | {'-'*20} | {'-'*25} | {'-'*30} |")
        for t in task.transitions:
            f = t.from_stage or "-"
            lines.append(f"| {t.at:<30} | {t.actor_harness}/{t.actor_model:<25} | {f:<20} | {t.to_stage:<25} | {(t.note or ''):<30} |")
        lines.append("")

    # Reviews
    if task.reviews:
        lines.append("* Reviews")
        lines.append("")
        for r in task.reviews:
            lines.append(f"- Verdict: {r.verdict} by {r.reviewer_harness}/{r.reviewer_model} at {r.at}")
            lines.append(f"  - Findings: {r.findings}")
            lines.append(f"  - Disposition: {r.disposition}")
        lines.append("")

    return "\n".join(lines)


def org_row(field: str, value: str, col_width: int, lines: list[str]) -> None:
    lines.append(f"| {field:<{col_width}} | {value:<60} |")
