"""Headless smoke tests for the Textual task board."""

import pytest
from textual.widgets import DataTable, Input, Label, Markdown

from hsd.tui.app import BoardApp


@pytest.mark.asyncio
async def test_board_builds_against_database_at_narrow_width(db, sample_task):
    app = BoardApp(db.db_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        table = app.screen.query_one("#task-table", DataTable)
        assert table.row_count == 1
        assert table.virtual_size.width <= 80
        assert [column.label.plain for column in table.columns.values()] == [
            "Slug", "Stage", "Status",
        ]
        assert sample_task.slug in table.get_row_at(0)
        assert sample_task.stage in table.get_row_at(0)
        assert "1 tasks" in str(app.screen.query_one("#board-stats", Label).content)

        await pilot.resize_terminal(110, 24)
        await pilot.pause()
        assert [column.label.plain for column in table.columns.values()] == [
            "Slug", "Stage", "Status", "Owner",
        ]


@pytest.mark.asyncio
async def test_board_opens_full_task_markdown_and_returns(db, sample_task):
    app = BoardApp(db.db_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        detail = app.screen.query_one("#task-detail", Markdown)
        assert "Test the system" in detail.source
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.query_one("#task-table", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_claim_prompts_with_user_and_claims_todo_task(db, sample_task, monkeypatch):
    monkeypatch.setenv("USER", "board-owner")
    app = BoardApp(db.db_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        assert app.screen.query_one("#owner", Input).value == "board-owner"
        await pilot.click("#claim")
        await pilot.pause()

        claimed = db.get_task(sample_task.slug)
        assert claimed.stage == "in-progress"
        assert claimed.owner_harness == "board-owner"
        assert claimed.owner_model == "tui"
