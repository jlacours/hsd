"""Shared fixtures for HSD tests."""

import os
import tempfile
import pytest

from hsd.core.db import Database


@pytest.fixture
def db() -> Database:
    """Create a Database backed by a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    os.unlink(db_path)


@pytest.fixture
def sample_task(db: Database) -> dict:
    """Create a sample task and return its details."""
    task = db.create_task(
        slug="test-task",
        title="Test Task",
        destination="any",
        sections={
            "objective": "Test the system",
            "current_state": "Nothing works",
        },
        source_harness="test-harness",
        source_model="test-model/v1",
    )
    return task
