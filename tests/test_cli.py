"""Tests for CLI database selection and command wiring."""

from click.testing import CliRunner

from hsd.cli import cli
from hsd.core.db import Database


def test_db_option_uses_requested_database(tmp_path, monkeypatch):
    requested_path = tmp_path / "requested.db"
    default_data_home = tmp_path / "default-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(default_data_home))

    result = CliRunner().invoke(
        cli,
        [
            "--db",
            str(requested_path),
            "create",
            "isolated-task",
            "Isolated Task",
            "--source-harness",
            "pytest",
            "--source-model",
            "test-model",
        ],
    )

    assert result.exit_code == 0, result.output
    assert Database(str(requested_path)).get_task("isolated-task") is not None
    assert not (default_data_home / "hsd" / "hsd.db").exists()
