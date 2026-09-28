"""Tests for CLI database selection and command wiring."""

import subprocess
import sys

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
            "--objective",
            "Exercise the requested database",
            "--current-state",
            "No task exists yet",
        ],
    )

    assert result.exit_code == 0, result.output
    assert Database(str(requested_path)).get_task("isolated-task") is not None
    assert not (default_data_home / "hsd" / "hsd.db").exists()


def test_create_rejects_missing_canonical_sections(tmp_path):
    result = CliRunner().invoke(
        cli,
        [
            "--db", str(tmp_path / "invalid.db"),
            "create", "missing-format", "Missing format",
            "--source-harness", "pytest",
            "--source-model", "test-model",
        ],
    )

    assert result.exit_code == 1
    assert "objective, current_state" in result.output


def test_update_validation_is_a_friendly_cli_error(tmp_path):
    db_path = tmp_path / "update.db"
    db = Database(str(db_path))
    db.create_task(
        slug="cli-update",
        title="CLI Update",
        destination="any",
        sections={"objective": "Keep this", "current_state": "Initial"},
        source_harness="pytest",
        source_model="test-model",
    )
    result = CliRunner().invoke(
        cli,
        ["--db", str(db_path), "update", "cli-update", "-s", "objective="],
    )
    assert result.exit_code == 1
    assert "Error: section 'objective' must not be empty" in result.output


def test_tui_help_does_not_import_textual_app():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from click.testing import CliRunner; from hsd.cli import cli; import sys; "
            "r = CliRunner().invoke(cli, ['tui', '--help']); "
            "assert r.exit_code == 0, r.output; "
            "assert 'interactive task board' in r.output; "
            "assert 'hsd.tui.app' not in sys.modules; "
            "r = CliRunner().invoke(cli, ['ls', '--help']); "
            "assert r.exit_code == 0, r.output; "
            "assert 'hsd.tui.app' not in sys.modules",
        ],
        cwd=".",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_hsd_db_path_environment_variable(monkeypatch, tmp_path):
    requested_path = tmp_path / "custom" / "board.db"
    monkeypatch.setenv("HSD_DB_PATH", str(requested_path))

    from hsd.core.db import get_default_db_path

    assert get_default_db_path() == str(requested_path)
    assert requested_path.parent.is_dir()
