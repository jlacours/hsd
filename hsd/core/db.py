"""Database connection management, schema, and CRUD operations."""

import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from hsd.core.db_connection import ConnectionMixin, _immediate, _utcnow
from hsd.core.db_schema import SCHEMA_SQL, TASKS_COLUMNS, TASKS_TABLE_BODY
from hsd.core.db_tasks import TaskQueriesMixin
from hsd.core.db_profiles import AgentProfileQueriesMixin
from hsd.core.db_workflow import WorkflowQueriesMixin
from hsd.core.models import Review, Section, Task, Transition
from hsd.core.rules import validate_transition
from hsd.core.secret_scan import validate_no_secrets


def get_default_db_path() -> str:
    """Return the configured database path, defaulting under XDG_DATA_HOME."""
    configured_path = os.environ.get("HSD_DB_PATH")
    if configured_path:
        path = Path(configured_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    data_home = os.environ.get(
        "XDG_DATA_HOME",
        os.path.join(os.path.expanduser("~"), ".local", "share"),
    )
    db_dir = Path(data_home) / "hsd"
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "hsd.db")


class Database(
    TaskQueriesMixin,
    WorkflowQueriesMixin,
    AgentProfileQueriesMixin,
    ConnectionMixin,
):
    """Manages the SQLite connection and provides CRUD operations.

    Thread-safe connection management using threading.local.
    Writes use BEGIN IMMEDIATE for safe concurrent access under WAL.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_default_db_path()
        self._local = threading.local()
        self._init_schema()
