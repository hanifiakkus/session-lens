"""session-lens: every project's sessions in one list.

The default screen of ``session-lens``. Upstream's session browser shows
one project; this one lists the whole archive, newest first, with a
project column, and opens a session the same ways (Enter reads it in the
TUI, ``h`` opens its HTML page, ``c`` resumes it in Claude Code). Esc goes
to the project selector, whose first row comes back here.

Archive/delete/restore stay in the per-project browser: they act on one
project's files and cache.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Optional, cast

from rich.markup import escape as escape_markup
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Footer, Header, Label

from .session_lens import load_theme, save_theme
from .tui_search import (
    SearchBar,
    SessionRow,
    all_sessions,
    focus_search_bar,
    open_session_page,
    project_label,
)
from .tui_viewer import ConversationScreen
from .utils import DEFAULT_THEME, normalize_theme

BACK_TO_PROJECTS = "back_to_projects"


def _local(ts: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:16]
    return when.astimezone().strftime(fmt)


class AllSessionsBrowser(App[Optional[str]]):
    """Sessions of every project, newest first."""

    CSS = """
    #main-container { padding: 0; height: 100%; }
    #stats-container {
        height: auto;
        min-height: 3;
        max-height: 5;
        border: solid $primary;
    }
    #sessions-table { height: 1fr; }
    """

    TITLE = "session-lens - All sessions"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "back_to_projects", "Projects"),
        Binding("enter", "view_session", "View"),
        Binding("h", "open_html", "Open HTML page"),
        Binding("c", "resume_selected", "Resume in Claude Code"),
        Binding("p", "back_to_projects", "Projects", show=False),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("slash", "focus_search", "Search", show=False),
    ]

    def __init__(
        self, projects_dir: Path, db_path: Path, html_theme: str = DEFAULT_THEME
    ) -> None:
        super().__init__()
        self.theme = load_theme(self)
        self.projects_dir = projects_dir
        self.db_path = db_path
        self.html_theme = normalize_theme(html_theme)
        self.rows: list[SessionRow] = []

    def watch_theme(self, theme: str) -> None:
        save_theme(theme)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Vertical():
                with Container(id="stats-container"):
                    yield Label("Loading sessions…", id="stats")
                yield SearchBar(self.projects_dir, None, self.html_theme)
                yield DataTable[str](id="sessions-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.rows = all_sessions(self.db_path)
        except Exception as e:  # noqa: BLE001 - shown in the stats box
            self.query_one("#stats", Label).update(f"Could not read the cache: {e}")
            return
        self._update_stats()
        self._populate()
        self.query_one("#sessions-table").focus()

    def on_resize(self) -> None:
        if self.rows:
            self._populate()

    # -- table ------------------------------------------------------------

    def _update_stats(self) -> None:
        projects = {r.project_path for r in self.rows}
        messages = sum(r.message_count for r in self.rows)
        tokens = sum(r.tokens for r in self.rows)
        firsts = [r.first_timestamp for r in self.rows if r.first_timestamp]
        lasts = [r.last_timestamp for r in self.rows if r.last_timestamp]
        span = ""
        if firsts and lasts:
            start, end = _local(min(firsts), "%Y-%m-%d"), _local(max(lasts), "%Y-%m-%d")
            span = start if start == end else f"{start} to {end}"
        self.query_one("#stats", Label).update(
            f"[bold]Project:[/bold] All projects ({len(projects)})   "
            f"[bold]Sessions:[/bold] {len(self.rows):,} | "
            f"[bold]Messages:[/bold] {messages:,} | "
            f"[bold]Tokens:[/bold] {tokens:,}   "
            f"[bold]Date Range:[/bold] {span or '-'}"
        )

    def _populate(self) -> None:
        table = cast(DataTable[str], self.query_one("#sessions-table", DataTable))
        row = table.cursor_row
        table.clear(columns=True)
        width = max(80, self.size.width)
        fixed = 24 + 16 + 16 + 9 + 11 + 6 * 2
        table.add_column("Project", width=24)
        table.add_column("Title or First Message", width=max(20, width - fixed - 4))
        table.add_column("Start Time", width=16)
        table.add_column("End Time", width=16)
        table.add_column("Messages", width=9)
        table.add_column("Tokens", width=11)
        for i, r in enumerate(self.rows):
            table.add_row(
                escape_markup(project_label(r.project_path)),
                escape_markup(r.name or "No preview available"),
                _local(r.first_timestamp),
                _local(r.last_timestamp),
                str(r.message_count),
                f"{r.tokens:,}" if r.tokens else "-",
                key=str(i),
            )
        if self.rows:
            table.move_cursor(row=min(max(row, 0), len(self.rows) - 1))

    def _selected(self) -> Optional[SessionRow]:
        table = cast(DataTable[str], self.query_one("#sessions-table", DataTable))
        if not self.rows or not (0 <= table.cursor_row < len(self.rows)):
            return None
        return self.rows[table.cursor_row]

    # -- actions ----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = cast(DataTable[str], event.data_table)  # pyright: ignore[reportUnknownMemberType]
        if table.id == "sessions-table":
            self.action_view_session()

    def action_view_session(self) -> None:
        row = self._selected()
        if row is None:
            self.notify("No session selected", severity="warning")
            return
        self.push_screen(
            ConversationScreen(
                Path(row.project_path),
                row.session_id,
                project_label=project_label(row.project_path),
            )
        )

    def action_open_html(self) -> None:
        row = self._selected()
        if row is None:
            self.notify("No session selected", severity="warning")
            return
        try:
            page = open_session_page(
                Path(row.project_path), row.session_id, self.html_theme
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            self.notify(f"Could not open the page: {e}", severity="error")
            return
        self.notify(f"Opened: {page.name}")

    def action_resume_selected(self) -> None:
        """Resume in Claude Code, from the session's own directory."""
        row = self._selected()
        if row is None:
            self.notify("No session selected", severity="warning")
            return
        if row.cwd:
            target = Path(row.cwd)
            if target.is_dir():
                os.chdir(target)
            else:
                self.notify(
                    f"Session directory not found: {row.cwd}", severity="warning"
                )
        try:
            with self.suspend():
                os.execvp("claude", ["claude", "-r", row.session_id])
        except FileNotFoundError:
            self.notify(
                "Claude Code CLI not found. Make sure 'claude' is in your PATH.",
                severity="error",
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            self.notify(f"Error resuming session: {e}", severity="error")

    def action_back_to_projects(self) -> None:
        self.exit(BACK_TO_PROJECTS)

    def action_focus_search(self) -> None:
        focus_search_bar(self)


def run_all_sessions_browser(
    projects_dir: Path, db_path: Path, theme: str = DEFAULT_THEME
) -> Optional[str]:
    app = AllSessionsBrowser(projects_dir, db_path, theme)
    try:
        return app.run()
    except KeyboardInterrupt:
        return None


__all__: list[Any] = [
    "AllSessionsBrowser",
    "BACK_TO_PROJECTS",
    "run_all_sessions_browser",
]
