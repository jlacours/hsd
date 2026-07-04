"""V1 file-pair importer — walks the old board and creates DB tasks."""

import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

from hsd.core.db import Database

logger = logging.getLogger(__name__)

STAGE_DIRS = {
    "todo": "todo",
    "in-progress": "in-progress",
    "done": "done",
    "reviewed": "reviewed",
    "to-be-revised-by-human": "to-be-revised-by-human",
}

# Harness directory names we look for
HARNESS_DIRS = [
    "for-claude-code",
    "for-codex",
    "for-hermes",
    "for-opencode",
    "for-pi-coding-agent",
    "for-any-harness",
]

SECTION_HEADING_RE = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
METADATA_LINE_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|")
TITLE_RE = re.compile(r"^#\s+Handoff:\s*(.+)$", re.MULTILINE)
SLUG_FILENAME_RE = re.compile(
    r"^\d{8}T\d{6}Z--(.+)\.md$"
)
TIMESTAMP_FILENAME_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z"
)


@dataclass
class MigrationResult:
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


class Migrator:
    """Import tasks from v1 file-pair board into the HSD database."""

    def __init__(self, db: Database):
        self.db = db

    def migrate(self, source_dir: str | None = None) -> MigrationResult:
        """Walk the v1 board and import tasks."""
        if source_dir is None:
            source_dir = os.path.expanduser("~/.harnesses_share_directory")

        source = Path(source_dir)
        if not source.is_dir():
            return MigrationResult(errors=1, error_details=[f"Source directory not found: {source}"])

        result = MigrationResult()

        for harness_dir in HARNESS_DIRS:
            hdir = source / harness_dir
            if not hdir.is_dir():
                continue

            # Determine the harness name from the directory
            harness_name = harness_dir.replace("for-", "", 1)

            for stage_name, stage_subdir in STAGE_DIRS.items():
                sdir = hdir / stage_subdir
                if not sdir.is_dir():
                    continue

                for md_file in sorted(sdir.glob("*.md")):
                    self._import_file(md_file, harness_name, stage_name, result)

        return result

    def _import_file(
        self,
        md_path: Path,
        harness_name: str,
        stage_name: str,
        result: MigrationResult,
    ) -> None:
        """Import a single .md handoff file."""
        try:
            content = md_path.read_text(encoding="utf-8")

            # Extract slug from filename
            slug = self._extract_slug(md_path)
            if slug is None:
                result.errors += 1
                result.error_details.append(f"Cannot extract slug from filename: {md_path}")
                return

            # Check if already imported
            existing = self.db.get_task(slug)
            if existing is not None:
                result.skipped += 1
                return

            # Extract metadata
            metadata = self._parse_metadata(content)
            title = self._extract_title(content) or metadata.get("title", slug)

            # Extract sections
            sections = self._parse_sections(content)

            # Create timestamp from filename
            created_at = self._extract_timestamp(md_path)

            # Determine source_harness and source_model from metadata
            source_harness = metadata.get("source harness", harness_name)
            source_model = metadata.get("model", "MODEL NOT EXPOSED")
            author = metadata.get("author/agent")
            working_dir = metadata.get("working directory")
            repository_meta = metadata.get("repository")
            branch_commit = metadata.get("branch/commit")
            tree_state = metadata.get("working tree")

            # Infer owner from board directory for non-todo stages
            owner_harness = None
            owner_model = None
            if stage_name != "todo" and harness_name != "any-harness":
                owner_harness = metadata.get("owner harness", harness_name)
                owner_model = metadata.get("owner model")

            # Create timestamp from filename for both timestamps
            file_ts = self._extract_timestamp(md_path)

            # Store original path as artifact
            artifacts = sections.get("artifacts", "")
            if artifacts:
                artifacts = f"- Original: {md_path}\n" + artifacts
            else:
                artifacts = f"- Original: {md_path}"
            sections["artifacts"] = artifacts

            # Ensure minimum required sections
            for required in ("objective", "current_state"):
                if required not in sections:
                    sections[required] = "(imported — content not parsed from v1)"

            # If nothing was parsed into known sections, put everything in raw
            if len(sections) <= 1:  # just artifacts
                sections["raw"] = content

            self.db.create_task(
                slug=slug,
                title=title,
                destination=harness_name,
                sections=sections,
                source_harness=source_harness,
                source_model=source_model,
                author=author,
                working_dir=working_dir,
                repository=repository_meta,
                branch_commit=branch_commit,
                tree_state=tree_state,
                stage=stage_name,
                updated_at=file_ts,
            )

            # Set owner directly via update if inferred
            if owner_harness:
                self.db._conn().execute(
                    "UPDATE tasks SET owner_harness = ?, owner_model = ? WHERE slug = ?",
                    (owner_harness, owner_model, slug),
                )
                self.db._conn().commit()

            result.imported += 1
            logger.info(f"Imported: {slug} from {md_path} (stage={stage_name}, owner={owner_harness})")

        except Exception as e:
            result.errors += 1
            result.error_details.append(f"{md_path}: {e}")
            logger.exception(f"Error importing {md_path}")

    @staticmethod
    def _extract_slug(md_path: Path) -> str | None:
        m = SLUG_FILENAME_RE.match(md_path.name)
        if m:
            return m.group(1)
        # Fallback: use stem
        return md_path.stem.replace("_", "-")

    @staticmethod
    def _parse_metadata(content: str) -> dict[str, str]:
        """Parse the metadata table from a v1 handoff."""
        metadata: dict[str, str] = {}
        in_table = False
        for line in content.split("\n"):
            if line.startswith("|") and ("---" not in line):
                m = METADATA_LINE_RE.match(line)
                if m:
                    key = m.group(1).strip().lower()
                    val = m.group(2).strip()
                    metadata[key] = val
                    in_table = True
            elif in_table and not line.startswith("|"):
                break  # end of table
        return metadata

    @staticmethod
    def _extract_title(content: str) -> str | None:
        m = TITLE_RE.search(content)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _parse_sections(content: str) -> dict[str, str]:
        """Parse ##-level sections from markdown content."""
        sections: dict[str, str] = {}
        # Split on ## headings
        parts = re.split(r"\n(?=##\s)", content)
        for part in parts:
            m = SECTION_HEADING_RE.match(part)
            if m:
                raw_heading = m.group(1).strip().lower()
                body = part[m.end():].strip()
                # Map heading names to section keys (raw heading has spaces)
                key = _map_heading_to_section(raw_heading)
                if key:
                    sections[key] = body
        return sections

    @staticmethod
    def _extract_timestamp(md_path: Path) -> str:
        """Extract ISO timestamp from the v1 filename."""
        m = TIMESTAMP_FILENAME_RE.match(md_path.stem)
        if m:
            return (
                f"{m.group(1)}-{m.group(2)}-{m.group(3)}T"
                f"{m.group(4)}:{m.group(5)}:{m.group(6)}Z"
            )
        return "2024-01-01T00:00:00Z"  # fallback


_HEADING_MAP: dict[str, str] = {
    "objective": "objective",
    "current state": "current_state",
    "summary for review": "summary_for_review",
    "work completed": "work_completed",
    "files changed": "files_changed",
    "commands and verification": "commands_verification",
    "decisions and assumptions": "decisions_assumptions",
    "blockers and risks": "blockers_risks",
    "warnings": "warnings",
    "next actions": "next_actions",
    "artifacts and references": "artifacts",
    "continuation prompt": "continuation_prompt",
    "plan": "raw",
}


def _map_heading_to_section(heading: str) -> str | None:
    """Map a markdown heading to a normalized section key."""
    h = heading.lower().strip()
    if h in _HEADING_MAP:
        return _HEADING_MAP[h]

    # Fuzzy match: check if any key is a substring
    for key, val in _HEADING_MAP.items():
        if key in h or h in key:
            return val

    return None
