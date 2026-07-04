"""Database connection management, schema, and CRUD operations."""

import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from hsd.core.models import Task, Section, Transition, Review

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    destination     TEXT NOT NULL,
    owner_harness   TEXT,
    owner_model     TEXT,
    stage           TEXT NOT NULL CHECK (stage IN
        ('todo','in-progress','done','reviewed','to-be-revised-by-human')),
    status          TEXT NOT NULL CHECK (status IN
        ('queued','in-progress','blocked','complete')),
    source_harness  TEXT NOT NULL,
    source_model    TEXT NOT NULL,
    model_check_note TEXT,
    author          TEXT,
    working_dir     TEXT,
    repository      TEXT,
    branch_commit   TEXT,
    tree_state      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    name      TEXT NOT NULL CHECK (name IN
        ('objective','current_state','summary_for_review','work_completed',
         'files_changed','commands_verification','decisions_assumptions',
         'blockers_risks','warnings','next_actions','artifacts',
         'continuation_prompt','raw')),
    content   TEXT NOT NULL,
    PRIMARY KEY (task_id, name)
);

CREATE TABLE IF NOT EXISTS transitions (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    at            TEXT NOT NULL,
    actor_harness TEXT NOT NULL,
    actor_model   TEXT NOT NULL,
    from_stage    TEXT,
    to_stage      TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY,
    task_id           INTEGER NOT NULL REFERENCES tasks(id),
    at                TEXT NOT NULL,
    reviewer_harness  TEXT NOT NULL,
    reviewer_model    TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (verdict IN
        ('accepted','changes-requested','human-revision-required')),
    findings          TEXT NOT NULL,
    disposition       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage);
CREATE INDEX IF NOT EXISTS idx_tasks_dest  ON tasks(destination, stage);
CREATE INDEX IF NOT EXISTS idx_trans_task  ON transitions(task_id, at);
"""


def get_default_db_path() -> str:
    """Return the default database path under XDG_DATA_HOME."""
    data_home = os.environ.get(
        "XDG_DATA_HOME",
        os.path.join(os.path.expanduser("~"), ".local", "share"),
    )
    db_dir = Path(data_home) / "hsd"
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "hsd.db")


class Database:
    """Manages the SQLite connection and provides CRUD operations.

    Thread-safe connection management using threading.local.
    Writes use BEGIN IMMEDIATE for safe concurrent access under WAL.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_default_db_path()
        self._local = threading.local()
        self._init_schema()

    @property
    def _raw_conn(self) -> sqlite3.Connection:
        """Get the raw connection without schema init (for polling)."""
        if not hasattr(self._local, "_raw_conn") or self._local._raw_conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local._raw_conn = conn
        return self._local._raw_conn

    def _init_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        conn = self._raw_conn
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        """Get a connection with schema guaranteed."""
        return self._raw_conn

    # --- Public helpers for web polling ---

    def data_version(self) -> int:
        """Return PRAGMA data_version for change detection.

        Uses a dedicated read-only connection so changes from our own writes
        don't pollute the polling signal.
        """
        # Use a separate connection to avoid same-connection blind spot
        if not hasattr(self._local, "_poll_conn") or self._local._poll_conn is None:
            poll = sqlite3.connect(self.db_path, timeout=5, uri=True)
            poll.execute("PRAGMA query_only=ON;")
            self._local._poll_conn = poll
        row = self._local._poll_conn.execute("PRAGMA data_version;").fetchone()
        return row[0] if row else 0

    # --- Task CRUD ---

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
    ) -> Task:
        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO tasks
                   (slug, title, destination, stage, status, source_harness,
                    source_model, model_check_note, author, working_dir,
                    repository, branch_commit, tree_state, created_at, updated_at)
                   VALUES (?, ?, ?, 'todo', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug, title, destination,
                    source_harness, source_model, model_check_note,
                    author, working_dir, repository, branch_commit, tree_state,
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
                   VALUES (?, ?, ?, ?, NULL, 'todo', 'task created')""",
                (task_id, now, source_harness, source_model),
            )
        return self.get_task(task_id)

    def get_task(self, slug_or_id: str | int) -> Task | None:
        conn = self._conn()
        if isinstance(slug_or_id, str):
            row = conn.execute(
                "SELECT * FROM tasks WHERE slug = ?", (slug_or_id,)
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
                for name, content in section_patches.items():
                    self._validate_section_name(name)
                    conn.execute(
                        """INSERT INTO sections (task_id, name, content)
                           VALUES (?, ?, ?)
                           ON CONFLICT(task_id, name) DO UPDATE SET content = excluded.content""",
                        (task.id, name, content),
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
                return None  # already claimed
            conn.execute(
                """INSERT INTO transitions (task_id, at, actor_harness, actor_model,
                   from_stage, to_stage, note)
                   VALUES (?, ?, ?, ?, 'todo', 'in-progress', ?)""",
                (task.id, now, harness, model, f"claimed by {harness}"),
            )
        return self.get_task(task.id)

    def transition_task(
        self,
        slug_or_id: str | int,
        to_stage: str,
        actor_harness: str,
        actor_model: str,
        note: str | None = None,
    ) -> Task | None:
        """Move a task to a new stage and record the transition."""
        now = _utcnow()
        task = self.get_task(slug_or_id)
        if task is None:
            return None
        conn = self._conn()
        with _immediate(conn):
            status_map = {
                "in-progress": "in-progress",
                "done": "complete",
                "reviewed": "complete",
                "to-be-revised-by-human": "blocked",
                "todo": "queued",
            }
            new_status = status_map.get(to_stage, task.status)
            conn.execute(
                "UPDATE tasks SET stage = ?, status = ?, updated_at = ? WHERE id = ?",
                (to_stage, new_status, now, task.id),
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
        """Record a review and route the task. Returns (task, error)."""
        task = self.get_task(slug_or_id)
        if task is None:
            return None, "task not found"
        if task.stage not in ("done", "reviewed", "to-be-revised-by-human"):
            return None, f"task is in '{task.stage}', not awaiting review"

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

    # --- Internal ---

    def _row_to_task(self, row: sqlite3.Row, conn: sqlite3.Connection) -> Task:
        d = dict(row)
        task_id = d["id"]
        section_rows = conn.execute(
            "SELECT name, content FROM sections WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        sections_list = [Section(name=r["name"], content=r["content"]) for r in section_rows]
        transitions_list = self.get_transitions(task_id)
        reviews_list = self.get_reviews(task_id)
        return Task(
            **d,
            sections=sections_list,
            transitions=transitions_list,
            reviews=reviews_list,
        )

    @staticmethod
    def _validate_section_name(name: str) -> None:
        valid = {
            "objective", "current_state", "summary_for_review", "work_completed",
            "files_changed", "commands_verification", "decisions_assumptions",
            "blockers_risks", "warnings", "next_actions", "artifacts",
            "continuation_prompt", "raw",
        }
        if name not in valid:
            raise ValueError(f"invalid section name: {name!r} (valid: {sorted(valid)})")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Generator[None, None, None]:
    """Execute a block inside BEGIN IMMEDIATE with rollback on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
