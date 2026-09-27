"""Markdown handoff renderer."""

from hsd.core.models import Task, Section

SECTION_TITLES: dict[str, str] = {
    "objective": "Objective",
    "plan": "Plan",
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
    """Render a Task as a complete markdown handoff document."""
    lines: list[str] = []
    lines.append(f"# Handoff: {task.title}")
    lines.append("")

    # Metadata table
    lines.append("## Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    md_row("Source harness", task.source_harness, lines)
    md_row("Model", task.source_model, lines)
    md_row("Destination", task.destination, lines)
    md_row("Stage", task.stage, lines)
    md_row("Status", task.status, lines)
    if task.owner_harness:
        md_row("Owner harness", task.owner_harness, lines)
    if task.owner_model:
        md_row("Owner model", task.owner_model, lines)
    if task.working_dir:
        md_row("Working directory", task.working_dir, lines)
    if task.branch_commit:
        md_row("Branch/commit", task.branch_commit, lines)
    if task.tree_state:
        md_row("Tree state", task.tree_state, lines)
    md_row("Created", task.created_at, lines)
    md_row("Updated", task.updated_at, lines)
    lines.append("")

    # Sections
    for section in task.sections:
        title = SECTION_TITLES.get(section.name, section.name.replace("_", " ").title())
        lines.append(f"## {title}")
        lines.append("")
        if section.name == "continuation_prompt":
            lines.append(f"> {section.content}")
        else:
            lines.append(section.content)
        lines.append("")

    # Transitions (timeline)
    if task.transitions:
        lines.append("## Timeline")
        lines.append("")
        lines.append("| When | Actor | From | To | Note |")
        lines.append("|---|---|---|---|---|")
        for t in task.transitions:
            f = t.from_stage or "-"
            lines.append(f"| {t.at} | {t.actor_harness}/{t.actor_model} | {f} | {t.to_stage} | {t.note or ''} |")
        lines.append("")

    # Reviews
    if task.reviews:
        lines.append("## Reviews")
        lines.append("")
        for r in task.reviews:
            lines.append(f"- **Verdict:** {r.verdict} by {r.reviewer_harness}/{r.reviewer_model} at {r.at}")
            lines.append(f"  - Findings: {r.findings}")
            lines.append(f"  - Disposition: {r.disposition}")
        lines.append("")

    return "\n".join(lines)


def render_section(section: Section) -> str:
    """Render a single section as markdown."""
    title = SECTION_TITLES.get(section.name, section.name.replace("_", " ").title())
    return f"## {title}\n\n{section.content}"


def md_row(field: str, value: str, lines: list[str]) -> None:
    lines.append(f"| {field} | {value} |")
