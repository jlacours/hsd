"""SQLite connection and transaction management."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone

from hsd.core.db_schema import SCHEMA_SQL, TASKS_COLUMNS, TASKS_TABLE_BODY
from hsd.core.models import Section, Task


class ConnectionMixin:
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
        self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Add columns to existing tables that predate them.

        Lightweight, additive migration so live databases created before a
        column existed upgrade transparently on next connection.
        """
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column in ("diff", "verify_cmd"):
            if column not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        conn.commit()
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()["sql"]
        if "'closed'" not in table_sql:
            self._rebuild_tasks_table(conn)

    def _rebuild_tasks_table(self, conn: sqlite3.Connection) -> None:
        """Rebuild the tasks table to pick up CHECK constraint changes.

        ALTER TABLE cannot modify CHECK constraints, so databases created
        before the 'closed' stage existed must be rebuilt following the
        standard SQLite procedure (sqlite.org/lang_altertable.html#otheralter).
        Task ids are copied verbatim -- sections/transitions/reviews
        reference tasks(id).
        """
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF;")
        try:
            with _immediate(conn):
                conn.execute(f"CREATE TABLE tasks_rebuild ({TASKS_TABLE_BODY})")
                conn.execute(
                    f"""INSERT INTO tasks_rebuild ({TASKS_COLUMNS})
                        SELECT {TASKS_COLUMNS} FROM tasks"""
                )
                conn.execute("DROP TABLE tasks")
                conn.execute("ALTER TABLE tasks_rebuild RENAME TO tasks")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_dest ON tasks(destination, stage)"
                )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"foreign key violations after tasks table rebuild: "
                    f"{[tuple(v) for v in violations]}"
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON;")

    def _conn(self) -> sqlite3.Connection:
        """Get a connection with schema guaranteed."""
        return self._raw_conn

    def data_version(self) -> int:
        """Return PRAGMA data_version for change detection.

        Uses a dedicated read-only connection so changes from our own writes
        don't pollute the polling signal.
        """
        if not hasattr(self._local, "_poll_conn") or self._local._poll_conn is None:
            poll = sqlite3.connect(self.db_path, timeout=5, uri=True)
            poll.execute("PRAGMA query_only=ON;")
            self._local._poll_conn = poll
        row = self._local._poll_conn.execute("PRAGMA data_version;").fetchone()
        return row[0] if row else 0

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
