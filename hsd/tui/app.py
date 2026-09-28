"""Interactive terminal board backed directly by the HSD SQLite data layer."""

from __future__ import annotations

import getpass
import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Markdown

from hsd.core.db import Database
from hsd.core.models import Task
from hsd.render.markdown import render_task


@contextmanager
def _database(db_path: str) -> Iterator[Database]:
    """Open a Database for one operation and release its SQLite connection."""
    db = Database(db_path)
    try:
        yield db
    finally:
        db.close()


class ClaimOwnerScreen(ModalScreen[str | None]):
    """Ask which owner should claim the selected task."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, owner: str) -> None:
        super().__init__()
        self.default_owner = owner

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="claim-dialog"):
                yield Label("Claim task as owner harness")
                yield Input(value=self.default_owner, placeholder="Owner", id="owner")
                yield Button("Claim", variant="primary", id="claim")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#claim")
    def submit(self) -> None:
        owner = self.query_one("#owner", Input).value.strip()
        if owner:
            self.dismiss(owner)
        else:
            self.notify("Owner cannot be empty", severity="warning", timeout=2)

    @on(Button.Pressed, "#cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#owner")
    def submit_input(self) -> None:
        self.submit()

    def action_cancel(self) -> None:
        self.dismiss(None)


class TaskDetailScreen(Screen[None]):
    """Scrollable detail page containing the complete Markdown task render."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("q", "go_back", "Back"),
    ]

    def __init__(self, task: Task) -> None:
        super().__init__()
        self.task_data = task

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Markdown(render_task(self.task_data), id="task-detail")
        yield Footer()

    def action_go_back(self) -> None:
        self.app.pop_screen()


class BoardScreen(Screen[None]):
    """Responsive task table and board summary."""

    BINDINGS = [
        Binding("j", "move_down", "Down", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("enter", "open_selected", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "claim_selected", "Claim"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path
        self._tasks: list[Task] = []
        self._wide: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield DataTable(id="task-table", cursor_type="row", zebra_stripes=True)
        yield Label("Loading board…", id="board-stats")
        yield Footer()

    def on_mount(self) -> None:
        self._configure_table()
        self.refresh_board()
        self.set_interval(5, self.refresh_board)

    def on_resize(self, event) -> None:
        # Rebuild only when the optional owner field changes visibility.
        wide = self.size.width >= 100
        if wide != self._wide:
            self._configure_table()
        self.refresh_board()

    def _configure_table(self) -> None:
        table = self.query_one(DataTable)
        wide = self.size.width >= 100
        self._wide = wide
        table.clear(columns=True)
        slug_width = max(18, min(44, self.size.width - (72 if wide else 50)))
        table.add_column("Slug", key="slug", width=slug_width)
        table.add_column("Stage", key="stage", width=17 if wide else 20)
        table.add_column("Status", key="status", width=15 if wide else 14)
        if wide:
            table.add_column("Owner", key="owner", width=18)

    def refresh_board(self) -> None:
        selected_slug: str | None = None
        table = self.query_one(DataTable)
        if table.row_count:
            row = min(table.cursor_row, len(self._tasks) - 1)
            if row >= 0:
                selected_slug = self._tasks[row].slug

        try:
            with _database(self.db_path) as db:
                tasks = db.list_tasks()
                stats = db.board_stats()
        except sqlite3.OperationalError:
            self.notify("Database is busy; retrying shortly.", severity="warning", timeout=3)
            return

        self._tasks = tasks
        table.clear()
        for task in tasks:
            values = [task.slug, task.stage, task.status]
            if self._wide:
                values.append(task.owner_harness or "-")
            table.add_row(*values, key=task.slug)

        if selected_slug:
            index = next((i for i, task in enumerate(tasks) if task.slug == selected_slug), None)
            if index is not None and tasks:
                table.move_cursor(row=index)
            elif tasks:
                table.move_cursor(row=0)
        elif tasks:
            table.move_cursor(row=0)

        by_stage = stats["by_stage"]
        stage_summary = "  ".join(
            f"{stage}: {count}" for stage, count in sorted(by_stage.items())
        ) or "no tasks"
        self.query_one("#board-stats", Label).update(f"{stats['total']} tasks  |  {stage_summary}")

    def action_move_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_move_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_refresh(self) -> None:
        self.refresh_board()

    def _selected_task(self) -> Task | None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self._tasks):
            return None
        return self._tasks[row]

    def action_open_selected(self) -> None:
        selected = self._selected_task()
        if selected is None:
            return
        try:
            with _database(self.db_path) as db:
                task = db.get_task(selected.slug)
        except sqlite3.OperationalError:
            self.notify("Database is busy; try opening the task again.", severity="warning", timeout=3)
            return
        if task is not None:
            self.app.push_screen(TaskDetailScreen(task))

    @on(DataTable.RowSelected, "#task-table")
    def open_selected_row(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if 0 <= row < len(self._tasks):
            self.query_one(DataTable).move_cursor(row=row)
            self.action_open_selected()

    def action_claim_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            self.notify("Select a task first", severity="warning", timeout=2)
        elif task.stage != "todo" or task.owner_harness:
            self.notify("Only unclaimed todo tasks can be claimed", severity="warning", timeout=3)
        else:
            default_owner = os.environ.get("USER") or getpass.getuser()
            self.app.push_screen(ClaimOwnerScreen(default_owner), self._claim_task(task.slug))

    def _claim_task(self, slug: str):
        def claimed(owner: str | None) -> None:
            if owner is None:
                return
            try:
                with _database(self.db_path) as db:
                    task = db.claim_task(slug, owner, "tui")
            except sqlite3.OperationalError:
                self.notify("Database is busy; the task was not claimed.", severity="warning", timeout=3)
                return
            if task is None:
                self.notify("Task was already claimed or is no longer todo", severity="warning", timeout=3)
            else:
                self.notify(f"Claimed {task.slug} for {owner}", timeout=3)
            self.refresh_board()

        return claimed

    def action_quit(self) -> None:
        self.app.exit()


class BoardApp(App[None]):
    """HSD terminal board."""

    TITLE = "HSD Task Board"
    CSS = """
    Screen { layout: vertical; }
    #task-table { height: 1fr; }
    #board-stats { height: 1; padding: 0 1; color: $text-muted; }
    #claim-dialog { width: 48; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #claim-dialog Input { margin: 1 0; }
    #claim-dialog Button { width: 100%; margin: 0 0 1 0; }
    #task-detail { height: 1fr; overflow-y: auto; padding: 0 1; }
    """

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path

    def on_mount(self) -> None:
        self.push_screen(BoardScreen(self.db_path))
