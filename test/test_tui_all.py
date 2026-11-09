"""Tests for the all-projects session list and the session-lens TUI loop."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import ensure_fresh_cache
from claude_code_log.session_lens import ALL_PROJECTS, ALL_PROJECTS_LABEL, run_tui
from claude_code_log.tui_all import BACK_TO_PROJECTS, AllSessionsBrowser
from claude_code_log.tui_search import all_sessions
from claude_code_log.tui_viewer import ConversationScreen

# The conftest skips test_tui_* files on unit runs by name; the mark is what
# puts them in the TUI run.
pytestmark = pytest.mark.tui


def _session(sid: str, prompt: str, minute: int) -> str:
    def entry(kind: str, uuid: str, parent: Any, message: dict) -> dict:
        return {
            "type": kind,
            "sessionId": sid,
            "uuid": uuid,
            "parentUuid": parent,
            "timestamp": f"2026-01-01T10:{minute:02d}:00Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/work",
            "version": "2.0.0",
            "message": message,
        }

    lines = [
        entry("user", f"{sid}-u", None, {"role": "user", "content": prompt}),
        entry(
            "assistant",
            f"{sid}-a",
            f"{sid}-u",
            {
                "id": f"m-{sid}",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        ),
    ]
    return "\n".join(json.dumps(e) for e in lines) + "\n"


OLD = "aaaaaaaa-0000-0000-0000-000000000001"
NEW = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two projects, one session each, cached in one database."""
    projects = tmp_path / "projects"
    db = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db))
    for name, sid, prompt, minute in (
        ("-work-alpha", OLD, "older alpha prompt", 1),
        ("-work-beta", NEW, "newer beta prompt", 30),
    ):
        project = projects / name
        project.mkdir(parents=True)
        (project / f"{sid}.jsonl").write_text(_session(sid, prompt, minute))
        manager = CacheManager(project, get_library_version(), db_path=db)
        ensure_fresh_cache(project, manager, silent=True)
    return projects, db


class TestAllSessions:
    def test_every_project_newest_first(self, archive):
        _projects, db = archive
        rows = all_sessions(db)
        assert [r.session_id for r in rows] == [NEW, OLD]
        assert rows[0].name == "newer beta prompt"
        assert rows[0].project_path.endswith("-work-beta")
        assert rows[0].tokens == 7


class TestAllSessionsBrowser:
    @pytest.mark.asyncio
    async def test_lists_all_and_enter_opens_the_viewer(self, archive):
        projects, db = archive
        app = AllSessionsBrowser(projects, db)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause(0.2)
            stats = str(app.query_one("#stats").render())
            assert "All projects (2)" in stats
            assert "Sessions: 2" in stats
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if isinstance(app.screen, ConversationScreen) and app.screen.loaded:
                    break
            screen = app.screen
            assert isinstance(screen, ConversationScreen)
            assert screen.session_id == NEW
            assert "newer beta prompt" in screen.title_text

    @pytest.mark.asyncio
    async def test_escape_goes_to_the_projects(self, archive):
        projects, db = archive
        app = AllSessionsBrowser(projects, db)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause()
        assert app.return_value == BACK_TO_PROJECTS


class TestProjectSelectorAllRow:
    @pytest.mark.asyncio
    async def test_first_row_is_all_with_the_total(self, archive):
        from textual.widgets import DataTable

        from claude_code_log.tui import ProjectSelector

        projects, _db = archive
        dirs = sorted(projects.iterdir())
        app = ProjectSelector(dirs, [], set(), include_all=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one("#projects-table", DataTable)
            first = table.get_row_at(0)
            assert str(first[0]) == ALL_PROJECTS_LABEL
            assert str(first[1]) == "2"
            await pilot.press("enter")
            await pilot.pause()
        assert app.return_value == ALL_PROJECTS

    @pytest.mark.asyncio
    async def test_upstream_selector_has_no_all_row(self, archive):
        from textual.widgets import DataTable

        from claude_code_log.tui import ProjectSelector

        projects, _db = archive
        app = ProjectSelector(sorted(projects.iterdir()), [], set())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one("#projects-table", DataTable)
            assert str(table.get_row_at(0)[0]) != ALL_PROJECTS_LABEL


class TestRunTui:
    def test_starts_on_all_and_esc_round_trips(self, archive, capsys):
        projects, _db = archive
        beta = projects / "-work-beta"
        calls: list[str] = []

        def all_browser(*_a: Any, **_k: Any) -> str | None:
            calls.append("all")
            # First visit: Esc to the projects; second: quit.
            return BACK_TO_PROJECTS if calls.count("all") == 1 else None

        selections = iter([beta, ALL_PROJECTS])

        def selector(*_a: Any, include_all: bool = False, **_k: Any) -> Path:
            assert include_all
            calls.append("selector")
            return next(selections)

        def project_browser(path: Path, **_k: Any) -> str:
            calls.append(f"project:{path.name}")
            return BACK_TO_PROJECTS

        with (
            patch("claude_code_log.tui_all.run_all_sessions_browser", all_browser),
            patch("claude_code_log.tui.run_project_selector", selector),
            patch("claude_code_log.cli._launch_tui_with_cache_check", project_browser),
        ):
            run_tui(projects, "minimal")
        assert calls == [
            "all",
            "selector",
            "project:-work-beta",
            "selector",
            "all",
        ]


class TestAllRowIsNotAProject:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["a", "d", "r"])
    async def test_archive_delete_restore_refuse_it(self, archive, key):
        from claude_code_log.tui import ProjectSelector

        projects, _db = archive
        app = ProjectSelector(sorted(projects.iterdir()), [], set(), include_all=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            assert app.selected_project_path == ALL_PROJECTS
            await pilot.press(key)
            await pilot.pause()
            # No confirmation screen: still on the selector's own screen.
            assert len(app.screen_stack) == 1


class TestSearchResultStats:
    def test_each_hit_carries_its_sessions_size(self, archive):
        from claude_code_log.tui_search import run_title_search, session_stats

        projects, db = archive
        results = run_title_search(db, "prompt", None)
        stats = session_stats(db, results)
        new = stats[NEW]
        assert new.tokens == 7
        assert new.messages >= 2
        assert new.size == (projects / "-work-beta" / f"{NEW}.jsonl").stat().st_size

    @pytest.mark.asyncio
    async def test_the_results_table_shows_them(self, archive):
        from textual.widgets import DataTable

        from claude_code_log.tui_search import SearchScreen

        projects, db = archive
        app = AllSessionsBrowser(projects, db)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("ctrl+f", *"beta")
            for _ in range(60):
                await pilot.pause(0.05)
                screen = app.screen
                if isinstance(screen, SearchScreen) and screen.results:
                    break
            screen = app.screen
            assert isinstance(screen, SearchScreen)
            table = screen.query_one("#search-results", DataTable)
            labels = [str(c.label) for c in table.columns.values()]
            assert labels[3:6] == ["Msgs", "Tokens", "Size"]
            row = [str(cell) for cell in table.get_row_at(0)]
            assert row[4] == "7"
            assert row[5].endswith(" B")
