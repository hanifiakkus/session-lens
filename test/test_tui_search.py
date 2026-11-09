"""Tests for the TUI search (session-lens): the bar, its layout, name search."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from claude_code_log.cache import SessionCacheData
from claude_code_log.tui import SessionBrowser
from claude_code_log.tui_search import (
    TITLE_HIT_TYPE,
    highlighted_folded,
    PrepareError,
    SearchBar,
    SearchScreen,
    run_title_search,
    session_name,
    title_matches,
    title_terms,
)

# The conftest skips test_tui_* files on unit runs by name; the mark is what
# puts them in the TUI run.
pytestmark = pytest.mark.tui


class TestSessionNames:
    def test_name_prefers_ai_title_then_summary_then_prompt(self):
        assert session_name("AI title", "summary", "prompt") == "AI title"
        assert session_name(None, "summary", "prompt") == "summary"
        assert session_name(None, None, "first\n  prompt") == "first prompt"
        assert session_name(None, None, None) == ""

    def test_every_term_must_appear_ignoring_case(self):
        terms = title_terms('Fix "Login"  bug')
        assert terms == ["fix", "login", "bug"]
        assert title_matches("Login BUG fix for the form", terms)
        assert not title_matches("Login fix", terms)

    def test_non_ascii_case_is_folded(self):
        assert title_matches("ÇALIŞMA notları", title_terms("çalışma"))
        assert title_matches("İSTANBUL ofisi", title_terms("istanbul"))
        assert title_matches("ılık su", title_terms("ILIK"))

    def test_empty_query_matches_nothing(self):
        assert title_terms('  ""  ') == []
        assert not title_matches("anything", [])


def _cache_db(path: Path) -> Path:
    db = path / "cache.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY, project_path TEXT);
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY, project_id INTEGER, session_id TEXT,
            summary TEXT, first_timestamp TEXT, last_timestamp TEXT,
            first_user_message TEXT, ai_title TEXT,
            hidden INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO projects VALUES (1, '/p/alpha'), (2, '/p/beta');
        INSERT INTO sessions
            (project_id, session_id, summary, last_timestamp,
             first_user_message, ai_title, hidden)
        VALUES
            (1, 's-old', NULL, '2026-01-01T00:00:00Z', 'deploy the api', NULL, 0),
            (1, 's-new', NULL, '2026-02-01T00:00:00Z', 'x', 'Deploy fix', 0),
            (2, 's-beta', 'Deploy docs', '2026-03-01T00:00:00Z', 'y', NULL, 0),
            (2, 's-hidden', NULL, '2026-04-01T00:00:00Z', 'deploy', NULL, 1),
            (2, 's-other', 'Unrelated', '2026-05-01T00:00:00Z', 'deploy', NULL, 0);
        """
    )
    conn.commit()
    conn.close()
    return db


class TestRunTitleSearch:
    def test_matches_the_shown_name_newest_first(self, tmp_path: Path):
        db = _cache_db(tmp_path)
        results = run_title_search(db, "deploy", None)
        # s-other's prompt says "deploy", but its shown name is its summary.
        assert [r.session_id for r in results] == ["s-beta", "s-new", "s-old"]
        assert all(r.type == TITLE_HIT_TYPE and r.message_uuid is None for r in results)
        assert results[1].snippet == "Deploy fix"

    def test_project_scope(self, tmp_path: Path):
        db = _cache_db(tmp_path)
        results = run_title_search(db, "deploy", Path("/p/alpha"))
        assert {r.session_id for r in results} == {"s-new", "s-old"}

    def test_blank_query(self, tmp_path: Path):
        assert run_title_search(_cache_db(tmp_path), "  ", None) == []


def _browser(project: Path) -> SessionBrowser:
    app = SessionBrowser(project)
    sessions = {
        "session-123": SessionCacheData(
            session_id="session-123",
            first_timestamp="2025-01-01T10:00:00Z",
            last_timestamp="2025-01-01T10:01:00Z",
            message_count=2,
            first_user_message="Hello",
        )
    }
    app.cache_manager.get_cached_project_data = Mock(  # type: ignore[method-assign]
        return_value=Mock(sessions=sessions, working_directories=[str(project)])
    )
    app.cache_manager.get_modified_files = Mock(return_value=[])  # type: ignore[method-assign]
    return app


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestSearchBarLayout:
    @pytest.mark.asyncio
    async def test_bar_is_a_framed_box_under_the_stats_and_nothing_scrolls(
        self, project_dir: Path
    ):
        app = _browser(project_dir)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            bar = app.query_one(SearchBar)
            stats = app.query_one("#stats-container")
            table = app.query_one("#sessions-table")
            # A framed box: one line of text inside a rounded border ...
            assert bar.region.height == 3
            assert bar.styles.border.top[0] == "round"
            # ... right under the stats and right above the table: the
            # frame's half-cells are the spacing, no blank lines added.
            assert bar.region.y == stats.region.bottom
            assert table.region.y == bar.region.bottom
            container = app.query_one("#main-container")
            assert container.max_scroll_y == 0
            assert not container.show_vertical_scrollbar
            assert app.screen.max_scroll_y == 0
            # The bar must not narrow the table (a side margin on it did).
            assert table.region.width == container.region.width

    @pytest.mark.asyncio
    async def test_list_keeps_focus_until_the_bar_is_asked_for(self, project_dir: Path):
        app = _browser(project_dir)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            assert not app.query_one(SearchBar).has_focus

    @pytest.mark.asyncio
    async def test_click_activates_the_bar(self, project_dir: Path):
        app = _browser(project_dir)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.click(SearchBar)
            await pilot.pause()
            bar = app.query_one(SearchBar)
            assert bar.has_focus
            assert bar.styles.border.top[0] == "round"  # Input's own :focus lost

    @pytest.mark.asyncio
    async def test_typing_opens_a_session_name_search(self, project_dir: Path):
        app = _browser(project_dir)
        with patch(
            "claude_code_log.tui_search.prepare_search",
            side_effect=PrepareError("test"),
        ) as prepare:
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.1)
                await pilot.press("slash", "d")
                await pilot.pause(0.2)
                screen = app.screen
                assert isinstance(screen, SearchScreen)
                assert not screen.full_text
                # A name search asks for the cache only, never the FTS index.
                assert prepare.call_args.args[3] is False
                await pilot.press("ctrl+r")
                await pilot.pause(0.1)
                assert screen.full_text

    @pytest.mark.asyncio
    async def test_ctrl_f_focuses_the_bar(self, project_dir: Path):
        app = _browser(project_dir)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("ctrl+f")
            await pilot.pause()
            assert app.query_one(SearchBar).has_focus

    @pytest.mark.asyncio
    async def test_ctrl_f_on_the_results_returns_to_the_box(self, project_dir: Path):
        app = _browser(project_dir)
        with patch(
            "claude_code_log.tui_search.prepare_search",
            side_effect=PrepareError("test"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.1)
                await pilot.press("ctrl+f", "d")
                await pilot.pause(0.2)
                screen = app.screen
                assert isinstance(screen, SearchScreen)
                screen.query_one("#search-results").focus()
                await pilot.pause()
                await pilot.press("ctrl+f")
                await pilot.pause()
                assert screen.query_one("#search-input").has_focus
                assert not screen.full_text  # ctrl+f no longer switches mode


class TestRememberedTheme:
    @pytest.mark.asyncio
    async def test_a_picked_theme_is_used_by_the_next_app(self, project_dir: Path):
        import claude_code_log.session_lens as session_lens

        # conftest points THEME_FILE at a temp file for the whole run.
        assert not str(session_lens.THEME_FILE).startswith(str(Path.home() / ".config"))
        app = _browser(project_dir)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app.theme = "dracula"
            await pilot.pause()
        assert session_lens.THEME_FILE.read_text().strip() == "dracula"
        app2 = _browser(project_dir)
        assert app2.theme == "dracula"

    def test_unknown_saved_theme_falls_back(self, project_dir: Path):
        import claude_code_log.session_lens as session_lens

        session_lens.THEME_FILE.write_text("no-such-theme")
        app = _browser(project_dir)
        assert app.theme == session_lens.FALLBACK_THEME


class TestHighlightFolded:
    def _marked(self, text: str, query: str) -> list[str]:
        rich_text = highlighted_folded(text, title_terms(query))
        return [text[s.start : s.end] for s in rich_text.spans]

    def test_turkish_i_variants_are_highlighted(self):
        assert self._marked("ılık su ve ILIK çay", "ilik") == ["ılık", "ILIK"]
        assert self._marked("İstanbul", "istanbul") == ["İstanbul"]

    def test_every_occurrence_of_every_term(self):
        assert self._marked("deploy a deploy b", "deploy b") == [
            "deploy",
            "deploy",
            "b",
        ]
