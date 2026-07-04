"""Transition rules, validation, and submission gate logic."""

from hsd.core.models import (
    ALLOWED_TRANSITIONS,
    REQUIRED_SUBMIT_SECTIONS,
    REVIEW_ROUTES,
    Task,
)


def validate_transition(
    task: Task,
    to_stage: str,
) -> tuple[bool, str]:
    """Check if a transition from the task's current stage to to_stage is legal.

    Returns (allowed, reason_if_denied).
    """
    # Check direct (from, to)
    if (task.stage, to_stage) in ALLOWED_TRANSITIONS:
        return True, ""

    # Check generic (None, to) — allows from any stage
    if (None, to_stage) in ALLOWED_TRANSITIONS:
        return True, ""

    return False, (
        f"transition from '{task.stage}' to '{to_stage}' is not allowed. "
        f"Allowed transitions: {_allowed_from(task.stage)}"
    )


def validate_submit_gate(task: Task) -> tuple[bool, str]:
    """Check that a task has all required sections before submission."""
    sections = task.sections_dict()
    missing = REQUIRED_SUBMIT_SECTIONS - set(sections.keys()) - {"raw"}
    # Also check content is non-empty
    empty = {
        name for name in REQUIRED_SUBMIT_SECTIONS
        if name in sections and not sections[name].strip()
    }
    errors = []
    if missing:
        errors.append(f"missing required sections: {', '.join(sorted(missing))}")
    if empty:
        errors.append(f"empty required sections: {', '.join(sorted(empty))}")
    if errors:
        return False, "; ".join(errors)
    return True, ""


def validate_no_self_review(
    task: Task,
    reviewer_harness: str,
    reviewer_model: str,
) -> tuple[bool, str]:
    """Check that the reviewer is not the owner (case-insensitive).

    Returns (allowed, warning_or_error).
    """
    if task.owner_harness and reviewer_harness.lower() == task.owner_harness.lower():
        return False, (
            f"self-review rejected: reviewer harness '{reviewer_harness}' "
            f"matches owner harness '{task.owner_harness}'"
        )
    # Warn if same model across different harnesses
    if (
        task.owner_model
        and reviewer_model.lower() == task.owner_model.lower()
        and reviewer_harness.lower() != (task.owner_harness or "").lower()
    ):
        return True, (
            f"warning: reviewer model '{reviewer_model}' matches owner model "
            f"from a different harness"
        )
    return True, ""


def validate_model_note(
    source_model: str,
    model_check_note: str | None,
) -> tuple[bool, str]:
    """MODEL NOT EXPOSED requires a model_check_note."""
    if source_model == "MODEL NOT EXPOSED" and not model_check_note:
        return False, (
            "source_model is 'MODEL NOT EXPOSED' but model_check_note is empty. "
            "Describe how the model identity was checked."
        )
    return True, ""


def get_review_route(verdict: str) -> str:
    """Return the target stage for a given review verdict."""
    return REVIEW_ROUTES.get(verdict, "to-be-revised-by-human")


def _allowed_from(stage: str) -> list[str]:
    """List stages that can be transitioned to from the given stage."""
    result = []
    for (from_stage, to_stage), reason in ALLOWED_TRANSITIONS.items():
        if from_stage is None or from_stage == stage:
            result.append(f"{stage} → {to_stage} ({reason})")
    return result
