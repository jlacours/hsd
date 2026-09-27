"""Canonical HSD task creation format shared by every interface."""

import re
from collections.abc import Mapping

from hsd.core.secret_scan import validate_no_secrets


CANONICAL_SECTION_NAMES = (
    "objective",
    "plan",
    "current_state",
    "summary_for_review",
    "work_completed",
    "files_changed",
    "commands_verification",
    "decisions_assumptions",
    "blockers_risks",
    "warnings",
    "next_actions",
    "artifacts",
    "continuation_prompt",
    "raw",
)

CREATION_REQUIRED_SECTIONS = ("objective", "current_state")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def canonicalize_slug(value: str) -> str:
    """Convert a legacy label or filename stem to a valid HSD slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        return "task"
    if not slug[0].isalpha():
        slug = f"task-{slug}"
    return slug


def validate_task_creation(
    *,
    slug: str,
    title: str,
    destination: str,
    sections: Mapping[str, str],
    source_harness: str,
    source_model: str,
    model_check_note: str | None,
) -> None:
    """Reject task creation data that does not follow the HSD contract."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"Invalid slug: {slug!r}. Slug must be kebab-case "
            "(lowercase letters, digits, and hyphens only)."
        )

    for label, value in (
        ("title", title),
        ("destination", destination),
        ("source_harness", source_harness),
        ("source_model", source_model),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")

    if source_model == "MODEL NOT EXPOSED" and not (model_check_note or "").strip():
        raise ValueError(
            "model_check_note is required when source_model is 'MODEL NOT EXPOSED'"
        )

    validate_task_sections(sections, require_creation_sections=True)


def validate_task_sections(
    sections: Mapping[str, str],
    *,
    require_creation_sections: bool = False,
) -> None:
    """Validate canonical section names, values, required content, and secrets."""
    if not isinstance(sections, Mapping):
        raise ValueError("sections must be a mapping of section name to Markdown content")

    invalid = sorted(set(sections) - set(CANONICAL_SECTION_NAMES))
    if invalid:
        raise ValueError(
            f"invalid section name: {invalid[0]!r} "
            f"(valid: {list(CANONICAL_SECTION_NAMES)})"
        )

    for name, content in sections.items():
        if not isinstance(content, str):
            raise ValueError(f"section {name!r} must contain text")
        if name in CREATION_REQUIRED_SECTIONS and not content.strip():
            raise ValueError(f"section {name!r} must not be empty")
        ok, error = validate_no_secrets(content)
        if not ok:
            raise ValueError(f"section {name!r}: {error}")

    if require_creation_sections:
        missing = [
            name
            for name in CREATION_REQUIRED_SECTIONS
            if name not in sections
        ]
        if missing:
            raise ValueError(
                "task creation requires non-empty sections: " + ", ".join(missing)
            )
