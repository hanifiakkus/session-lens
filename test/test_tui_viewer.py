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
        assert compact.title == "Compacted"
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
    async def test_the_prompt_above_is_pinned_while_scrolling_up(self, project):
        """Claude Code pins the prompt that started what is on screen once it
        scrolls out of sight; here only while scrolling up, never at the
        bottom. Clicking the pinned line goes to the prompt."""
        project_dir, manager = project
        app = _Host.make(ConversationScreen(project_dir, SESSION, manager))
        async with app.run_test(size=(100, 12)) as pilot:
            screen = await _open(pilot, app)
            view = screen.view
            prompt = next(i for i, b in enumerate(screen.blocks) if b.kind == "user")

            def top_line() -> str:
                return "".join(seg.text for seg in view.render_line(0))

            view.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            assert view.scroll_y > 0  # at the end, the prompt far above
            assert view.sticky_prompt() is None  # never at the bottom

            view.scroll_to(y=view.scroll_y - 2, animate=False, immediate=True)
            await pilot.pause()
            assert view.sticky_prompt() == prompt
            assert top_line().startswith(" \u276f Fix the failing test in app.py")

            view.scroll_to(y=view.scroll_y + 1, animate=False, immediate=True)
            await pilot.pause()
            assert view.sticky_prompt() is None  # reading on downwards

            view.scroll_to(y=view.scroll_y - 1, animate=False, immediate=True)
            await pilot.pause()
            await pilot.click(view, offset=(5, 0))
            await pilot.pause()
            assert view.scroll_y == view.top_of(prompt)
            assert view.sticky_prompt() is None
            assert top_line().startswith(" \u276f Fix the failing test")  # itself

    def test_sticky_text_is_the_first_paragraph(self):
        from claude_code_log.tui_viewer import STICKY_MAX, sticky_text

        assert sticky_text("\n  fix   this\nnow\n\nmore detail") == "fix this now"
        assert len(sticky_text("x" * 2000)) == STICKY_MAX

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


class TestClaudeCodeLook:
    def _strips(self, block: Block, **state: Any):
        from claude_code_log.tui_viewer import BlockState, Palette, StripRenderer

        return StripRenderer(80, Palette()).render(block, BlockState(**state))

    @staticmethod
    def _styles(strips: Any):
        return [seg.style for strip in strips for seg in strip if seg.style]

    def test_user_prompt_sits_on_claude_codes_grey(self):
        styles = self._styles(self._strips(Block(kind="user", body="hello")))
        assert any(
            s.bgcolor and s.bgcolor.triplet and tuple(s.bgcolor.triplet) == (55, 55, 55)
            for s in styles
        )

    def test_tool_dot_is_claude_codes_green_or_red(self):
        ok = self._styles(self._strips(Block(kind="tool", title="Bash(ls)")))
        bad = self._styles(
            self._strips(Block(kind="tool", title="Bash(ls)", error=True))
        )

        def has(styles: Any, rgb: tuple[int, int, int]) -> bool:
            return any(
                s.color and s.color.triplet and tuple(s.color.triplet) == rgb
                for s in styles
            )

        assert has(ok, (78, 186, 101))
        assert has(bad, (255, 107, 128))

    def test_an_empty_thinking_still_opens_and_says_why(self):
        from claude_code_log.tui_viewer import NO_THINKING_TEXT

        block = Block(kind="thinking", title="Thinking…", body="")
        assert block.collapsible
        closed = "".join(seg.text for strip in self._strips(block) for seg in strip)
        opened = "".join(
            seg.text for strip in self._strips(block, expanded=True) for seg in strip
        )
        assert NO_THINKING_TEXT not in closed
        assert NO_THINKING_TEXT in opened

    def test_no_dim_reaches_textual(self):
        """Rich 15 draws a Markdown rule dim; Textual cannot dim against the
        terminal's own background and crashed. Every dim is resolved first."""
        from rich.theme import Theme

        from claude_code_log.tui_viewer import BlockState, Palette, StripRenderer

        renderer = StripRenderer(80, Palette())
        renderer.console.push_theme(Theme({"markdown.hr": "dim"}))
        block = Block(
            kind="assistant",
            body="before\n\n---\n\nafter",
            markdown=True,
            preview=10**9,
        )
        strips = renderer.render(block, BlockState())
        segments = [seg for strip in strips for seg in strip]
        # The rule is there (Rich 14 draws it with "─", Rich 15 with "-").
        assert any("───" in seg.text or "---" in seg.text for seg in segments)
        assert not any(seg.style and seg.style.dim for seg in segments)


class TestClaudeCodeMarkdown:
    """Headings, tables and inline styles drawn as Claude Code does, the
    same under every Rich release (Rich 15 paints ## magenta)."""

    MD = (
        "# One\n\n## Two\n\n### Three\n\nUse `code` here.\n\n"
        "- first\n- second\n\n"
        "| Name | What | Ok |\n|---|---|:---:|\n"
        "| lens | a long description that has to wrap inside its cell | yes |\n"
    )

    def _lines(self, width: int = 60):
        from claude_code_log.tui_viewer import BlockState, Palette, StripRenderer

        block = Block(kind="assistant", body=self.MD, markdown=True, preview=10**9)
        return StripRenderer(width, Palette()).render(block, BlockState())

    @staticmethod
    def _segment(strips: Any, text: str):
        return next(seg for strip in strips for seg in strip if seg.text == text)

    def test_headings_are_left_aligned_bold_and_uncoloured(self):
        strips = self._lines()
        text = ["".join(seg.text for seg in strip) for strip in strips]
        assert text[0].startswith(" ● One")
        assert any(line.startswith("   Two") for line in text)
        one = self._segment(strips, "One").style
        assert one.bold and one.italic and one.underline
        for name in ("Two", "Three"):
            style = self._segment(strips, name).style
            assert style.bold and not style.underline
            assert tuple(style.color.triplet) == (255, 255, 255)

    def test_inline_code_has_claude_codes_permission_colour(self):
        style = self._segment(self._lines(), "code").style
        assert tuple(style.color.triplet) == (177, 185, 249)
        assert style.bgcolor is None

    def test_list_items_are_dashes(self):
        text = ["".join(seg.text for seg in strip) for strip in self._lines()]
        assert any(line.startswith("   - first") for line in text)

    def test_tables_are_fully_boxed_and_wrap(self):
        text = ["".join(seg.text for seg in strip).rstrip() for strip in self._lines()]
        table = [
            line.strip() for line in text if line.strip()[:1] in ("┌", "│", "├", "└")
        ]
        assert table[0].startswith("┌") and table[0].endswith("┐")
        assert table[-1].startswith("└") and table[-1].endswith("┘")
        assert all(
            line.startswith("│") and line.endswith("│")
            for line in table
            if line[0] == "│"
        )
        assert sum(line.startswith("│") for line in table) > 2  # the long cell wrapped


class TestCompaction:
    """Every compaction's summary is found and starts closed, as Claude Code
    shows it, including the ones Claude Code opens with a notice."""

    PREAMBLE = (
        "<artifact-content-authored-by-others/>\n"
        "The summarized conversation included Artifact content written by "
        "people other than you, which the summary may restate.\n"
    )

    def test_a_summary_after_a_notice_is_still_a_summary(self):
        from claude_code_log.factories.user_factory import (
            is_compacted_summary_text,
        )

        summary = f"{COMPACTED_SUMMARY_PREFIX}. Summary: work."
        assert is_compacted_summary_text(summary)
        assert is_compacted_summary_text(self.PREAMBLE + summary)
        # A prompt that quotes the line is not a summary.
        assert not is_compacted_summary_text(f"Why does it say\n{summary}")
        assert not is_compacted_summary_text("<b>" + "x" * 2000 + "\n" + summary)

    def test_viewer_closes_it_and_counts_its_tokens(self, tmp_path: Path):
        project_dir = tmp_path / "-work-app"
        project_dir.mkdir()
        boundary = {
            **_user("b1", "u1", 1, "x"),
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "level": "info",
            "compactMetadata": {"trigger": "auto", "preTokens": 120500},
        }
        boundary.pop("message")
        summary = _user(
            "s1",
            "b1",
            2,
            f"{self.PREAMBLE}{COMPACTED_SUMMARY_PREFIX}. Summary: the needle.",
        )
        summary["isCompactSummary"] = True
        lines = [_user("u1", None, 0, "start"), boundary, summary]
        (project_dir / f"{SESSION}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8"
        )
        manager = CacheManager(project_dir, "1.0.0-test", db_path=tmp_path / "c.db")
        blocks = load_blocks(project_dir, SESSION, manager, refresh=True)
        compact = [b for b in blocks if b.kind == "compact"]
        assert len(compact) == 1
        assert compact[0].title == "Compacted · 120.5k tokens summarized"
        assert "needle" in compact[0].body
        assert not any(b.kind == "user" and "needle" in b.body for b in blocks)
        assert find_matches(blocks, "needle") == [blocks.index(compact[0])]
