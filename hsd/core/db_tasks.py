"""Task creation, lookup, update, and claim queries."""

import uuid

from hsd.core.db_connection import _immediate, _utcnow
from hsd.core.models import Task
from hsd.core.task_format import validate_task_creation, validate_task_sections


class TaskQueriesMixin:
    def create_task(
        self,
        slug: str,
        title: str,
        destination: str,
        sections: dict[str, str],
        source_harness: str,
        source_model: str,
        model_check_note: str | None = None,
        author: str | None = None,
        working_dir: str | None = None,
        repository: str | None = None,
        branch_commit: str | None = None,
        tree_state: str | None = None,
        stage: str = "todo",
        updated_at: str | None = None,
    ) -> Task:
        validate_task_creation(
            slug=slug,
            title=title,
            destination=destination,
            sections=sections,
            source_harness=source_harness,
            source_model=source_model,
            model_check_note=model_check_note,
        )
        now = updated_at or _utcnow()
        herdr_session = f"hsd-{uuid.uuid4().hex[:16]}"
        status_map = {
            "todo": "queued",
            "in-progress": "in-progress",
            "done": "complete",
            "reviewed": "complete",
            "to-be-revised-by-human": "blocked",
            "closed": "complete",
        }
        status = status_map.get(stage, "queued")
        conn = self._conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO tasks
                   (slug, title, destination, stage, status, source_harness,
                    source_model, model_check_note, author, working_dir,
                    repository, branch_commit, tree_state, herdr_session,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug, title, destination, stage, status,
                    source_harness, source_model, model_check_note,
                    author, working_dir, repository, branch_commit, tree_state,
                    herdr_session,
                    now, now,
                ),
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for name, content in sections.items():
                self._validate_section_name(name)
                conn.execute(
                    "INSERT INTO sections (task_id, name, content) VALUES (?, ?, ?)",
                    (task_id, name, content),
                )
            conn.execute(
                """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
                   from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, NULL, ?, 'task created')""",
                (task_id, now, source_harness, source_model, stage),
            )
        return self.get_task(task_id)

    def get_task(self, slug_or_id: str | int) -> Task | None:
        conn = self._conn()
        if isinstance(slug_or_id, str):
            row = conn.execute(
                "SELECT * FROM tasks WHERE slug = ?", (slug_or_id,)
            ).fetchone()
            if row is None and slug_or_id.isdigit():
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (int(slug_or_id),)
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (slug_or_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row, conn)

    def list_tasks(
        self,
        harness: str | None = None,
        stage: str | None = None,
        destination: str | None = None,
    ) -> list[Task]:
        conn = self._conn()
        clauses: list[str] = ["1=1"]
        params: list = []
        if harness:
            clauses.append("owner_harness = ?")
            params.append(harness)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        if destination:
            clauses.append("destination = ?")
            params.append(destination)
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [self._row_to_task(r, conn) for r in rows]

    def update_task(
        self,
        slug_or_id: str | int,
        section_patches: dict[str, str] | None = None,
        status: str | None = None,
        owner_harness: str | None = None,
        owner_model: str | None = None,
    ) -> Task | None:
        now = _utcnow()
        conn = self._conn()
        task = self.get_task(slug_or_id)
        if task is None:
            return None
        with _immediate(conn):
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (now, task.id),
            )
            if status is not None:
                conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (status, task.id),
                )
            if owner_harness is not None:
                conn.execute(
                    "UPDATE tasks SET owner_harness = ? WHERE id = ?",
                    (owner_harness, task.id),
                )
            if owner_model is not None:
                conn.execute(
                    "UPDATE tasks SET owner_model = ? WHERE id = ?",
                    (owner_model, task.id),
                )
            if section_patches:
                validate_task_sections(section_patches)
                for name, content in section_patches.items():
                    self._validate_section_name(name)
                    conn.execute(
                        """INSERT INTO sections (task_id, name, content)
                           VALUES (?, ?, ?)
                           ON CONFLICT(task_id, name) DO UPDATE SET content = excluded.content""",
                        (task.id, name, content),
                    )
        return self.get_task(task.id)

    def update_plan_if_current(
        self,
        slug_or_id: str | int,
        expected_plan: str,
        new_plan: str,
    ) -> Task | None:
        """Atomically update a plan only when the caller edited the current text."""
        validate_task_sections({"plan": new_plan})
        task = self.get_task(slug_or_id)
        if task is None:
            return None
        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            row = conn.execute(
                "SELECT content FROM sections WHERE task_id = ? AND name = 'plan'",
                (task.id,),
            ).fetchone()
            current_plan = row["content"] if row else ""
            if current_plan != expected_plan:
                return None
            conn.execute(
                """INSERT INTO sections (task_id, name, content)
                   VALUES (?, 'plan', ?)
                   ON CONFLICT(task_id, name) DO UPDATE SET content = excluded.content""",
                (task.id, new_plan),
            )
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (now, task.id),
            )
        return self.get_task(task.id)

    def claim_task(self, slug_or_id: str | int, harness: str, model: str) -> Task | None:
        """Atomically claim a task. Returns None if already claimed."""
        now = _utcnow()
        task = self.get_task(slug_or_id)
        if task is None:
            return None
        conn = self._conn()
        with _immediate(conn):
            cursor = conn.execute(
                """UPDATE tasks
                   SET owner_harness = ?, owner_model = ?,
                       stage = 'in-progress', status = 'in-progress',
                       updated_at = ?
                   WHERE id = ? AND stage = 'todo' AND owner_harness IS NULL""",
                (harness, model, now, task.id),
            )
            if cursor.rowcount == 0:
                return None
            conn.execute(
                """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
                   from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, 'todo', 'in-progress', ?)""",
                (task.id, now, harness, model, f"claimed by {harness}"),
            )
        return self.get_task(task.id)
