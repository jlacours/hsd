"""Domain models for HSD tasks, sections, transitions, and reviews."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Section:
    name: str
    content: str


@dataclass
class Transition:
    id: int
    task_id: int
    at: str
    actor_harness: str
    actor_model: str
    from_stage: str | None
    to_stage: str
    note: str | None = None


@dataclass
class Review:
    id: int
    task_id: int
    at: str
    reviewer_harness: str
    reviewer_model: str
    verdict: str
    findings: str
    disposition: str


@dataclass
class Task:
    id: int
    slug: str
    title: str
    destination: str
    owner_harness: str | None
    owner_model: str | None
    stage: str
    status: str
    source_harness: str
    source_model: str
    created_at: str
    updated_at: str
    model_check_note: str | None = None
    author: str | None = None
    working_dir: str | None = None
    repository: str | None = None
    branch_commit: str | None = None
    tree_state: str | None = None
    sections: list[Section] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    def sections_dict(self) -> dict[str, str]:
        return {s.name: s.content for s in self.sections}

    @property
    def age_hours(self) -> float:
        """Age of the task in hours since creation."""
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - created).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0.0

    @property
    def staleness_hours(self) -> float:
        """Hours since last update."""
        try:
            updated = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - updated).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0.0


# Transition matrix: maps (from_stage, to_stage) -> allowed
# None as from_stage means "any"
ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("todo", "in-progress"): "claim",
    ("in-progress", "done"): "submit",
    ("done", "reviewed"): "approve",
    ("done", "todo"): "changes-requested",
    ("done", "to-be-revised-by-human"): "human-revision-required",
    ("in-progress", "to-be-revised-by-human"): "escalate",
    ("todo", "to-be-revised-by-human"): "escalate",
    ("to-be-revised-by-human", "todo"): "human-resolve",
}

# Review verdicts and their target stages
REVIEW_ROUTES: dict[str, str] = {
    "accepted": "reviewed",
    "changes-requested": "todo",
    "human-revision-required": "to-be-revised-by-human",
}

# Sections required before submit_for_review
REQUIRED_SUBMIT_SECTIONS = {
    "summary_for_review",
    "work_completed",
    "commands_verification",
    "next_actions",
}
