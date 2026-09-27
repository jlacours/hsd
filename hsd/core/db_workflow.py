"""Task transitions, reviews, and board summary queries."""

from hsd.core.db_connection import _immediate, _utcnow
from hsd.core.models import Review, Task, Transition
from hsd.core.rules import validate_submit_gate, validate_transition
from hsd.core.secret_scan import validate_no_secrets
from hsd.core.task_format import validate_task_sections


class WorkflowQueriesMixin:
    def submit_task_for_review(
        self,
        slug_or_id: str | int,
        *,
        actor_harness: str,
        actor_model: str,
        sections: dict[str, str],
        diff: str | None = None,
        verify_cmd: str | None = None,
        note: str = "submitted for review",
    ) -> Task | None:
        """Patch review sections and submit in one all-or-nothing transaction."""
        validate_task_sections(sections)
        if diff is not None:
            ok, reason = validate_no_secrets(diff)
            if not ok:
                raise ValueError(reason)

        conn = self._conn()
        with _immediate(conn):
            task = self.get_task(slug_or_id)
            if task is None:
                return None
            if (
                task.owner_harness
                and actor_harness.lower() != task.owner_harness.lower()
            ):
                raise ValueError(
                    f"submit_for_review denied: harness {actor_harness!r} "
                    f"does not match owner {task.owner_harness!r}"
                )
            ok, reason = validate_transition(task, "done")
            if not ok:
                raise ValueError(reason)

            for name, content in sections.items():
                conn.execute(
                    """INSERT INTO sections (task_id, name, content)
                       VALUES (?, ?, ?)
                       ON CONFLICT(task_id, name)
                       DO UPDATE SET content = excluded.content""",
                    (task.id, name, content),
                )

            candidate = self.get_task(task.id)
            ok, reason = validate_submit_gate(candidate)
            if not ok:
                raise ValueError(f"Submit gate: {reason}")

            now = _utcnow()
            conn.execute(
                """UPDATE tasks
                   SET stage = 'done', status = 'complete', updated_at = ?
                   WHERE id = ?""",
                (now, task.id),
            )
            if diff is not None:
                conn.execute("UPDATE tasks SET diff = ? WHERE id = ?", (diff, task.id))
            if verify_cmd is not None:
                conn.execute(
                    "UPDATE tasks SET verify_cmd = ? WHERE id = ?",
                    (verify_cmd, task.id),
                )
            conn.execute(
                """INSERT INTO transitions
                   (task_id, at, actor_harness, actor_model,
                    from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, ?, 'done', ?)""",
                (task.id, now, actor_harness, actor_model, task.stage, note),
            )
        return self.get_task(task.id)

    def transition_task(
        self,
        slug_or_id: str | int,
        to_stage: str,
        actor_harness: str,
        actor_model: str,
        note: str | None = None,
        diff: str | None = None,
        verify_cmd: str | None = None,
    ) -> Task | None:
        """Move a task to a new stage and record the transition.

        Validates the transition against the transition matrix via
        validate_transition. Raises ValueError for illegal moves.
        Returns None if the task is not found.

        diff and verify_cmd are only meaningful for the submit transition
        (to_stage == 'done'). When provided there, they are persisted on the
        task row (overwriting any previous values on resubmit), and diff is
        run through the secret scan just like section content is — a hit
        raises ValueError the same way an illegal transition does.
        """
        task = self.get_task(slug_or_id)
        if task is None:
            return None
        ok, reason = validate_transition(task, to_stage)
        if not ok:
            raise ValueError(reason)
        if to_stage == "done" and diff is not None:
            ok, reason = validate_no_secrets(diff)
            if not ok:
                raise ValueError(reason)
        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            status_map = {
                "in-progress": "in-progress",
                "done": "complete",
                "reviewed": "complete",
                "to-be-revised-by-human": "blocked",
                "todo": "queued",
                "closed": "complete",
            }
            new_status = status_map.get(to_stage, task.status)
            if to_stage == "todo":
                conn.execute(
                    """UPDATE tasks
                       SET stage = ?, status = ?, owner_harness = NULL,
                           owner_model = NULL, updated_at = ?
                       WHERE id = ?""",
                    (to_stage, new_status, now, task.id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET stage = ?, status = ?, updated_at = ? WHERE id = ?",
                    (to_stage, new_status, now, task.id),
                )
            if to_stage == "done":
                if diff is not None:
                    conn.execute(
                        "UPDATE tasks SET diff = ? WHERE id = ?", (diff, task.id),
                    )
                if verify_cmd is not None:
                    conn.execute(
                        "UPDATE tasks SET verify_cmd = ? WHERE id = ?", (verify_cmd, task.id),
                    )
            conn.execute(
                """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
                   from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task.id, now, actor_harness, actor_model, task.stage, to_stage, note),
            )
        return self.get_task(task.id)

    def add_review(
        self,
        slug_or_id: str | int,
        reviewer_harness: str,
        reviewer_model: str,
        verdict: str,
        findings: str,
        disposition: str,
    ) -> tuple[Task | None, str | None]:
        """Record a review and route the task. Returns (task, error).

        Only tasks in 'done' stage may be reviewed.
        On 'changes-requested', owner fields are cleared and destination is set
        to the previous owner so the task can be re-claimed.
        """
        task = self.get_task(slug_or_id)
        if task is None:
            return None, "task not found"
        if task.stage != "done":
            return None, (
                f"task is in '{task.stage}'; only tasks in 'done' stage "
                f"can be reviewed"
            )

        if reviewer_harness.lower() == (task.owner_harness or "").lower():
            return None, "self-review rejected: reviewer harness matches owner harness"

        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO reviews (task_id, at, reviewer_harness, reviewer_model,
                   verdict, findings, disposition)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task.id, now, reviewer_harness, reviewer_model, verdict, findings, disposition),
            )
            to_stage = {
                "accepted": "reviewed",
                "changes-requested": "todo",
                "human-revision-required": "to-be-revised-by-human",
            }[verdict]

            if verdict == "changes-requested":
                prev_owner = task.owner_harness or "any"
                conn.execute(
                    """UPDATE tasks SET stage = ?, destination = ?,
                       owner_harness = NULL, owner_model = NULL,
                       updated_at = ? WHERE id = ?""",
                    (to_stage, prev_owner, now, task.id),
                )
            elif verdict == "human-revision-required":
                conn.execute(
                    """UPDATE tasks SET stage = ?, owner_harness = NULL,
                       owner_model = NULL, updated_at = ? WHERE id = ?""",
                    (to_stage, now, task.id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET stage = ?, updated_at = ? WHERE id = ?",
                    (to_stage, now, task.id),
                )

            review_note = f"review verdict: {verdict}"
            conn.execute(
                """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
                   from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task.id, now, reviewer_harness, reviewer_model, task.stage, to_stage, review_note),
            )
        return self.get_task(task.id), None

    def get_transitions(self, task_id: int) -> list[Transition]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM transitions WHERE task_id = ? ORDER BY at ASC",
            (task_id,),
        ).fetchall()
        return [Transition(**dict(r)) for r in rows]

    def get_reviews(self, task_id: int) -> list[Review]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY at ASC",
            (task_id,),
        ).fetchall()
        return [Review(**dict(r)) for r in rows]

    def board_stats(self) -> dict:
        conn = self._conn()
        stage_counts = {
            r["stage"]: r["count"]
            for r in conn.execute(
                "SELECT stage, COUNT(*) as count FROM tasks GROUP BY stage"
            ).fetchall()
        }
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        return {
            "total": total,
            "by_stage": stage_counts,
        }
