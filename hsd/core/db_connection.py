"""SQLite connection and transaction management."""

import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone

from hsd.core.db_schema import (
    SCHEMA_SQL,
    SECTIONS_TABLE_BODY,
    TASK_FORMAT_TRIGGERS_SQL,
    TASKS_COLUMNS,
    TASKS_TABLE_BODY,
)
from hsd.core.models import Section, Task
from hsd.core.task_format import CANONICAL_SECTION_NAMES, CREATION_REQUIRED_SECTIONS


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
        additive_columns = {
            "diff": "TEXT",
            "verify_cmd": "TEXT",
            "herdr_session": "TEXT",
            "objective_section": "TEXT NOT NULL DEFAULT 'objective'",
            "current_state_section": "TEXT NOT NULL DEFAULT 'current_state'",
        }
        for column, definition in additive_columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        rows = conn.execute(
            "SELECT id, herdr_session FROM tasks ORDER BY id"
        ).fetchall()
        seen_sessions: set[str] = set()
        for row in rows:
            session = row["herdr_session"]
            if not session or session in seen_sessions:
                session = self._new_herdr_session(seen_sessions)
                conn.execute(
                    "UPDATE tasks SET herdr_session = ? WHERE id = ?",
                    (session, row["id"]),
                )
            seen_sessions.add(session)
        conn.commit()
        self._backfill_creation_sections(conn)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()["sql"]
        if (
            "'closed'" not in table_sql
            or "FOREIGN KEY (id, objective_section)" not in table_sql
            or not self._herdr_constraints_are_canonical(conn)
        ):
            self._rebuild_tasks_table(conn)
        sections_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sections'"
        ).fetchone()["sql"]
        if "'plan'" not in sections_sql:
            self._rebuild_sections_table(conn)
        conn.executescript(TASK_FORMAT_TRIGGERS_SQL)
        conn.commit()

    @staticmethod
    def _new_herdr_session(existing: set[str]) -> str:
        """Return a random task-scoped Herdr session absent from ``existing``."""
        while True:
            candidate = f"hsd-{uuid.uuid4().hex[:16]}"
            if candidate not in existing:
                return candidate

    @staticmethod
    def _herdr_constraints_are_canonical(conn: sqlite3.Connection) -> bool:
        """Check constraints that ALTER TABLE cannot add to legacy columns."""
        columns = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(tasks)")
        }
        column = columns.get("herdr_session")
        if column is None or column["notnull"] != 1:
            return False
        for index in conn.execute("PRAGMA index_list(tasks)").fetchall():
            if index["unique"] != 1:
                continue
            indexed = conn.execute(
                f"PRAGMA index_info({index['name']!r})"
            ).fetchall()
            if [row["name"] for row in indexed] == ["herdr_session"]:
                return True
        return False

    @staticmethod
    def _backfill_creation_sections(conn: sqlite3.Connection) -> None:
        """Bring legacy tasks up to the minimum canonical creation shape."""
        placeholders = {
            "objective": "(legacy task — objective not recorded)",
            "current_state": "(legacy task — current state not recorded)",
        }
        with _immediate(conn):
            for name in CREATION_REQUIRED_SECTIONS:
                conn.execute(
                    """INSERT INTO sections (task_id, name, content)
                       SELECT tasks.id, ?, ?
                       FROM tasks
                       LEFT JOIN sections
                         ON sections.task_id = tasks.id AND sections.name = ?
                       WHERE sections.task_id IS NULL""",
                    (name, placeholders[name], name),
                )
                conn.execute(
                    """UPDATE sections SET content = ?
                       WHERE name = ? AND trim(content) = ''""",
                    (placeholders[name], name),
                )

    def _rebuild_sections_table(self, conn: sqlite3.Connection) -> None:
        """Expand the sections CHECK constraint for the authoring plan."""
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF;")
        try:
            with _immediate(conn):
                conn.execute(f"CREATE TABLE sections_rebuild ({SECTIONS_TABLE_BODY})")
                conn.execute(
                    """INSERT INTO sections_rebuild (task_id, name, content)
                       SELECT task_id, name, content FROM sections"""
                )
                conn.execute("DROP TABLE sections")
                conn.execute("ALTER TABLE sections_rebuild RENAME TO sections")
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"foreign key violations after sections table rebuild: "
                        f"{[tuple(v) for v in violations]}"
                    )
        finally:
            conn.execute("PRAGMA foreign_keys=ON;")

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
        d.pop("objective_section", None)
        d.pop("current_state_section", None)
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
        if name not in CANONICAL_SECTION_NAMES:
            raise ValueError(
                f"invalid section name: {name!r} "
                f"(valid: {list(CANONICAL_SECTION_NAMES)})"
            )


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
