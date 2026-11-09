"""Tests for the Claude Code-style session viewer (session-lens)."""

import json
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.cache import CacheManager
from claude_code_log.factories import COMPACTED_SUMMARY_PREFIX
from claude_code_log.tui_viewer import (
    Block,
    ConversationScreen,
    find_matches,
    load_blocks,
)

# The conftest skips test_tui_* files on unit runs by name; the mark is what
# puts them in the TUI run.
pytestmark = pytest.mark.tui

SESSION = "11111111-2222-3333-4444-555555555555"
CWD = "/work/app"


def _entry(kind: str, uuid: str, parent: str | None, ts: int, **extra: Any) -> dict:
    entry: dict[str, Any] = {
        "type": kind,
        "sessionId": SESSION,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-01-01T10:{ts:02d}:00Z",
        "isSidechain": False,
        "userType": "external",
        "cwd": CWD,
        "version": "2.0.0",
    }
    entry.update(extra)
    return entry


def _user(uuid: str, parent: str | None, ts: int, content: Any, **extra: Any) -> dict:
    return _entry(
        "user", uuid, parent, ts, message={"role": "user", "content": content}, **extra
    )


def _assistant(uuid: str, parent: str, ts: int, item: dict) -> dict:
    return _entry(
        "assistant",
        uuid,
        parent,
        ts,
        message={
            "id": f"msg-{uuid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _tool_result(uuid: str, parent: str, ts: int, tool_id: str, text: str, **kw: Any):
    return _user(
        uuid,
        parent,
        ts,
        [{"type": "tool_result", "tool_use_id": tool_id, "content": text, **kw}],
    )


LONG_OUTPUT = "\n".join(f"output line {i}" for i in range(1, 11))

ENTRIES = [
    _user("u1", None, 0, "Fix the failing test in app.py"),
    _assistant("a1", "u1", 1, {"type": "thinking", "thinking": "Look at the test."}),
    _assistant("a2", "a1", 2, {"type": "text", "text": "I'll run the **tests**."}),
    _assistant(
        "a3",
        "a2",
        3,
        {
            "type": "tool_use",
            "id": "t1",
            "name": "Bash",
            "input": {"command": "pytest -q"},
        },
    ),
    _tool_result("r1", "a3", 4, "t1", LONG_OUTPUT),
    _assistant(
        "a4",
        "r1",
        5,
        {
            "type": "tool_use",
            "id": "t2",
            "name": "Edit",
            "input": {
                "file_path": f"{CWD}/app.py",
                "old_string": "x = 1\n",
                "new_string": "x = 2\n",
            },
        },
    ),
    _tool_result("r2", "a4", 6, "t2", "boom: file changed", is_error=True),
    _user("u2", "r2", 7, "[Request interrupted by user]"),
    _user(
        "u3",
        "u2",
        8,
        f"{COMPACTED_SUMMARY_PREFIX}. Summary: we fixed the needle test.",
    ),
    _assistant("a5", "u3", 9, {"type": "text", "text": "Done, all green."}),
]

SIDECHAIN = _assistant("s1", "a3", 4, {"type": "text", "text": "sub-agent internals"})
SIDECHAIN["isSidechain"] = True


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, CacheManager]:
    project_dir = tmp_path / "-work-app"
    project_dir.mkdir()
    lines = [*ENTRIES, SIDECHAIN]
    (project_dir / f"{SESSION}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8"
    )
    manager = CacheManager(project_dir, "1.0.0-test", db_path=tmp_path / "cache.db")
    return project_dir, manager


def _blocks(project: tuple[Path, CacheManager]) -> list[Block]:
    project_dir, manager = project
    return load_blocks(project_dir, SESSION, manager, refresh=True)


class TestBuildBlocks:
    def test_the_chat_reads_in_order_with_claude_codes_marks(self, project):
        blocks = _blocks(project)
        kinds = [b.kind for b in blocks]
        assert kinds == [
            "user",
            "thinking",
            "assistant",
            "tool",
            "tool",
            "interrupted",
            "compact",
            "assistant",
        ]

    def test_tool_call_and_result_are_one_block(self, project):
        bash = _blocks(project)[3]
        assert bash.title == "Bash(pytest -q)"
        assert bash.body == LONG_OUTPUT
        assert bash.collapsible  # 10 lines, 3 shown
        assert {"a3", "r1"} <= set(bash.uuids)

    def test_edit_is_an_update_with_a_relative_path_and_an_error(self, project):
        edit = _blocks(project)[4]
        assert edit.title == "Update(app.py)"
        assert edit.error
        assert "boom" in edit.body

    def test_sidechain_messages_are_hidden(self, project):
        assert not any("sub-agent" in b.search_text for b in _blocks(project))

    def test_compaction_summary_folds_away(self, project):
        compact = _blocks(project)[6]
        assert compact.title == "Conversation compacted"
        assert "needle" in compact.body
        assert compact.collapsible

    def test_find_matches_whole_words_in_any_block(self, project):
        blocks = _blocks(project)
        assert find_matches(blocks, "needle") == [6]
        assert find_matches(blocks, "OUTPUT line 10") == [3]
        assert find_matches(blocks, "  ") == []


class _Host:
    """A bare app to push the screen on."""

    @staticmethod
    def make(screen: ConversationScreen):
        from textual.app import App

        class Host(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        return Host()


async def _open(pilot: Any, app: Any) -> ConversationScreen:
    for _ in range(100):
        await pilot.pause(0.05)
        if isinstance(app.screen, ConversationScreen) and app.screen.loaded:
            break
    screen = app.screen
    assert isinstance(screen, ConversationScreen)
    assert screen.loaded
    return screen


class TestConversationScreen:
    @pytest.mark.asyncio
    async def test_opens_at_the_end_and_escape_closes(self, project):
        project_dir, manager = project
        app = _Host.make(ConversationScreen(project_dir, SESSION, manager))
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await _open(pilot, app)
            view = screen.view
            assert view.scroll_y == view.max_scroll_y
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ConversationScreen)

    @pytest.mark.asyncio
    async def test_click_expands_a_long_result(self, project):
        project_dir, manager = project
        app = _Host.make(ConversationScreen(project_dir, SESSION, manager))
        async with app.run_test(size=(100, 60)) as pilot:
            screen = await _open(pilot, app)
            view = screen.view
            index = next(i for i, b in enumerate(screen.blocks) if b.kind == "tool")
            before = len(view.strips[index] or [])
            top = view.top_of(index)
            assert top is not None
            view.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click(view, offset=(5, top - int(view.scroll_y)))
            await pilot.pause()
            assert view.states[index].expanded
            assert len(view.strips[index] or []) > before

    @pytest.mark.asyncio
    async def test_ctrl_f_finds_and_escape_closes_find_first(self, project):
        project_dir, manager = project
        app = _Host.make(ConversationScreen(project_dir, SESSION, manager))
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await _open(pilot, app)
            await pilot.press("ctrl+f", *"needle", "enter")
            await pilot.pause(0.3)
            assert screen.matches == [6]
            assert screen.view.states[6].current
            # The match was inside the collapsed summary: find opened it.
            assert screen.view.states[6].expanded
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConversationScreen)  # find closed
            assert not screen.view.states[6].expanded  # and closed it again
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ConversationScreen)

    @pytest.mark.asyncio
    async def test_opens_at_a_message_with_its_search(self, project):
        project_dir, manager = project
        screen = ConversationScreen(
            project_dir, SESSION, manager, target_uuid="r1", find="output line"
        )
        app = _Host.make(screen)
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app)
            assert (
                screen.matches
                and screen.blocks[screen.matches[screen.current]].kind == "tool"
            )

    @pytest.mark.asyncio
    async def test_title_is_the_session_name_not_its_id(self, project):
        # Opened from a message search hit, the screen knows only the id.
        project_dir, manager = project
        screen = ConversationScreen(
            project_dir, SESSION, manager, project_label="~/work/app"
        )
        app = _Host.make(screen)
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(pilot, app)
            title = str(screen.query_one("#conv-title").render())
            assert title.startswith("Fix the failing test in app.py")
            assert "2026-01-01" in title
            assert title.endswith("~/work/app")
            assert SESSION[:8] not in title


class TestOpeningFromTheSessionList:
    @pytest.mark.asyncio
    async def test_enter_opens_the_viewer_and_list_keys_are_off(self, project):
        from textual.widgets import DataTable

        from claude_code_log.tui import SessionBrowser

        project_dir, manager = project
        app = SessionBrowser(project_dir)
        app.cache_manager = manager
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.5)
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            await pilot.press("enter")
            screen = await _open(pilot, app)
            assert screen.session_id == SESSION
            # "d" deletes a session in the list; the modal viewer blocks it.
            await pilot.press("d")
            await pilot.pause()
            assert app.screen is screen
