"""session-lens: read a session the way Claude Code shows it.

Claude Code's chat screen, not its Ctrl+O transcript: user prompts behind
``>``, Claude's replies behind ``●``, each tool call as ``● Name(arg)`` with
its result under ``⎿`` cut to a few lines, thinking as ``✻ Thinking…``.
Long results, thinking and compaction summaries open on a click.

The conversation comes from upstream's format-neutral message tree
(``generate_template_messages``), the same one the HTML and Markdown
outputs use, so every special case they handle (slash commands, compaction
summaries, system reminders, specialised tool outputs) arrives already
parsed. ``build_blocks`` flattens that tree into ``Block``s, which is the
part worth testing; ``ConversationScreen`` only draws them.
"""

from __future__ import annotations

import difflib
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import Footer, Input, Static
from textual.worker import Worker

from .models import (
    AssistantTextMessage,
    BashInputMessage,
    BashOutput,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    EditInput,
    EditOutput,
    GlobOutput,
    ImageContent,
    MultiEditInput,
    ReadOutput,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskOutput,
    TextContent,
    ThinkingMessage,
    TodoWriteInput,
    ToolResultContent,
    ToolResultMessage,
    ToolUseContent,
    ToolUseMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserTextMessage,
    WriteInput,
    WriteOutput,
)

if TYPE_CHECKING:
    from .cache import CacheManager
    from .models import TranscriptEntry
    from .renderer import RenderingContext, TemplateMessage

#: Result lines shown before "… +N lines", as Claude Code does.
PREVIEW_LINES = 3

#: Claude Code's names for tools whose display differs from the API name.
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "Edit": "Update",
    "MultiEdit": "Update",
    "NotebookEdit": "Update",
    "Grep": "Search",
    "Glob": "Search",
    "WebFetch": "Fetch",
    "WebSearch": "Web Search",
    "TodoWrite": "Update Todos",
    "Agent": "Task",
}

#: Input fields worth showing in ``Name(…)``, most telling first.
_ARG_FIELDS = (
    "command",
    "file_path",
    "notebook_path",
    "pattern",
    "url",
    "query",
    "description",
    "skill",
    "path",
    "prompt",
)

_TODO_MARKS = {"completed": "☒", "in_progress": "◼", "pending": "☐"}

_INTERRUPTED = "[Request interrupted by user"


def _current_worker() -> Worker[None]:
    from .tui_search import _current_worker as current  # pyright: ignore[reportPrivateUsage]

    return current()


# -- blocks ---------------------------------------------------------------


@dataclass
class Block:
    """One visual unit of the conversation.

    ``kind`` picks the look: ``user``, ``assistant``, ``tool``, ``thinking``,
    ``compact``, ``note`` (system and command output), ``interrupted`` and
    ``branch``.
    """

    kind: str
    title: str = ""
    body: str = ""
    #: Render ``body`` as Markdown (assistant prose, agent answers).
    markdown: bool = False
    error: bool = False
    #: Colour ``body`` lines as a diff (``+``/``-``).
    diff: bool = False
    #: Body lines shown while collapsed; the rest open on a click.
    preview: int = PREVIEW_LINES
    #: Transcript entry uuids this block shows, for "open at this message".
    uuids: tuple[str, ...] = ()
    _folded: Optional[str] = field(default=None, repr=False, compare=False)

    @property
    def collapsible(self) -> bool:
        if self.kind in ("thinking", "compact"):
            return bool(self.body)
        return len(self.body.splitlines()) > self.preview

    @property
    def search_text(self) -> str:
        return f"{self.title}\n{self.body}"

    def folded(self) -> str:
        """``search_text`` folded for matching, computed once: a long
        session's tool output is megabytes, refolded on every keystroke."""
        if self._folded is None:
            self._folded = _fold(self.search_text)
        return self._folded


def _text_of(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif isinstance(item, ImageContent):
            parts.append("[Image]")
        # System reminders and IDE notifications: Claude Code hides them.
    return "\n".join(p for p in parts if p).strip()


def _plain(text: str) -> str:
    """Strip ANSI escapes (hook and command output carries colours)."""
    return Text.from_ansi(text).plain if "\x1b" in text else text


_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_SUMMARY = re.compile(r"^\s*<summary>(.*)</summary>\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _clean_markdown(md: str) -> str:
    """Upstream's Markdown for a message, as plain lines for the terminal."""
    lines: list[str] = []
    for line in md.splitlines():
        if _FENCE.match(line):
            continue
        if m := _SUMMARY.match(line):
            line = m.group(1)
        line = _TAG.sub("", line)
        if line.strip() or (lines and lines[-1].strip()):
            lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _relative(path: str, cwd: str) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def _input_fields(tool_input: Any) -> dict[str, Any]:
    if isinstance(tool_input, ToolUseContent):
        return dict(tool_input.input)
    dump = getattr(tool_input, "model_dump", None)
    if callable(dump):
        return cast(dict[str, Any], dump())
    return dict(getattr(tool_input, "__dict__", {}))


def tool_title(content: ToolUseMessage, cwd: str = "") -> str:
    """``Bash(git status)``, ``Update(src/app.py)``, ``Update Todos``."""
    name = TOOL_DISPLAY_NAMES.get(content.tool_name, content.tool_name)
    if content.tool_name == "TodoWrite":
        return name
    fields = _input_fields(content.input)
    for key in _ARG_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            arg = " ".join(value.split())
            if key in ("file_path", "notebook_path", "path"):
                arg = _relative(arg, cwd)
            if content.tool_name in ("Grep", "Glob"):
                arg = f'pattern: "{arg}"'
            if len(arg) > 120:
                arg = arg[:119] + "…"
            return f"{name}({arg})"
    return name


def _diff_lines(old: str, new: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", n=1
        )
        if not line.startswith(("---", "+++", "@@"))
    ]


def _diff_summary(path: str, lines: list[str]) -> str:
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    parts: list[str] = []
    if added:
        parts.append(f"{added} addition{'s' if added != 1 else ''}")
    if removed:
        parts.append(f"{removed} removal{'s' if removed != 1 else ''}")
    return f"Updated {path}" + (f" with {' and '.join(parts)}" if parts else "")


def _result_text(output: Any) -> str:
    if isinstance(output, ToolResultContent):
        if isinstance(output.content, str):
            return _plain(output.content)
        return "\n".join(
            str(item.get("text", "")) for item in output.content if "text" in item
        ).strip()
    for attr in ("content", "result", "message"):
        value = getattr(output, attr, None)
        if isinstance(value, str) and value:
            return _plain(value)
    return ""


def _agent_done(output: TaskOutput) -> str:
    meta = output.metadata
    if meta is None:
        return "Done"
    parts: list[str] = []
    if meta.tool_uses is not None:
        parts.append(f"{meta.tool_uses} tool use{'s' if meta.tool_uses != 1 else ''}")
    if meta.total_tokens is not None:
        parts.append(f"{meta.total_tokens / 1000:.1f}k tokens")
    if meta.duration_ms is not None:
        parts.append(f"{meta.duration_ms / 1000:.0f}s")
    return f"Done ({' · '.join(parts)})" if parts else "Done"


def tool_block(
    use: ToolUseMessage,
    result: Optional[ToolResultMessage],
    cwd: str = "",
    fallback: str = "",
) -> Block:
    """The ``● Name(arg)`` line and its ``⎿`` result, as Claude Code draws them.

    ``fallback`` is upstream's Markdown for the result, used for outputs
    with no plain-text field.
    """
    block = Block(kind="tool", title=tool_title(use, cwd))
    block.uuids = (use.meta.uuid,) + ((result.meta.uuid,) if result else ())
    tool_input = use.input
    output = result.output if result is not None else None

    if result is not None and result.is_error:
        block.error = True
        block.body = _result_text(output) or _clean_markdown(fallback)
        return block

    if isinstance(tool_input, TodoWriteInput):
        block.body = "\n".join(
            f"{_TODO_MARKS.get(t.status, '☐')} {t.content}" for t in tool_input.todos
        )
        block.preview = max(PREVIEW_LINES, len(tool_input.todos))
        return block

    if isinstance(output, EditOutput) or isinstance(
        tool_input, (EditInput, MultiEditInput)
    ):
        path = _relative(getattr(tool_input, "file_path", ""), cwd)
        lines: list[str] = []
        if isinstance(output, EditOutput) and output.diffs:
            for d in output.diffs:
                lines += _diff_lines(d.old_text, d.new_text)
        elif isinstance(tool_input, EditInput):
            lines = _diff_lines(tool_input.old_string, tool_input.new_string)
        elif isinstance(tool_input, MultiEditInput):
            for e in tool_input.edits:
                lines += _diff_lines(e.old_string, e.new_string)
        block.body = "\n".join([_diff_summary(path, lines), *lines])
        block.diff = True
        block.preview = 1 + 8
        return block

    if isinstance(output, ReadOutput):
        block.body = f"Read {output.num_lines} lines\n{output.content}"
        block.preview = 1
        return block

    if isinstance(tool_input, WriteInput) or isinstance(output, WriteOutput):
        content = tool_input.content if isinstance(tool_input, WriteInput) else ""
        path = _relative(getattr(tool_input, "file_path", ""), cwd)
        n = len(content.splitlines())
        block.body = f"Wrote {n} line{'s' if n != 1 else ''} to {path}\n{content}"
        block.preview = 1 + 4
        return block

    if isinstance(output, GlobOutput):
        n = len(output.files)
        block.body = "\n".join(
            [f"Found {n} file{'s' if n != 1 else ''}", *output.files]
        )
        block.preview = 1
        return block

    if isinstance(output, TaskOutput):
        block.body = f"{_agent_done(output)}\n{output.result}"
        block.preview = 1
        return block

    if isinstance(output, BashOutput):
        block.body = _plain(output.content)
        return block

    if output is not None:
        block.body = _result_text(output) or _clean_markdown(fallback)
    return block


def _is_hidden(msg: TemplateMessage) -> bool:
    """Sub-agent internals: Claude Code's chat shows only their spawn."""
    return msg.meta.is_sidechain or msg.agent_depth > 0 or msg.in_workflow_sidechannel


class _Builder:
    def __init__(self, ctx: RenderingContext) -> None:
        from .markdown.renderer import MarkdownRenderer

        self.ctx = ctx
        self.md = MarkdownRenderer(image_export_mode="placeholder")
        self.md._ctx = ctx  # pyright: ignore[reportPrivateUsage]
        self.blocks: list[Block] = []

    def markdown_of(self, msg: TemplateMessage) -> str:
        try:
            return self.md.format_content(msg)
        except Exception:  # noqa: BLE001 - a fallback must not break the view
            return ""

    def add(self, block: Optional[Block], *msgs: TemplateMessage) -> None:
        if block is None:
            return
        if not block.uuids:
            block.uuids = tuple(m.meta.uuid for m in msgs if m.meta.uuid)
        self.blocks.append(block)

    def walk(self, msg: TemplateMessage) -> None:
        if msg.is_middle_in_pair or msg.is_last_in_pair or _is_hidden(msg):
            return
        partners: list[TemplateMessage] = []
        if msg.is_first_in_pair:
            for index in (msg.pair_middle, msg.pair_last):
                if index is not None and (partner := self.ctx.get(index)):
                    partners.append(partner)

        content = msg.content
        if isinstance(content, ToolUseMessage):
            result = next(
                (
                    p
                    for p in partners
                    if isinstance(p.content, ToolResultMessage)
                    and p.content.tool_use_id == content.tool_use_id
                ),
                None,
            )
            result_content = cast(ToolResultMessage, result.content) if result else None
            fallback = self.markdown_of(result) if result else ""
            self.add(tool_block(content, result_content, msg.meta.cwd, fallback))
            partners = [p for p in partners if p is not result]
        else:
            self.add(self.block_for(msg), msg)

        for partner in partners:
            if not _is_hidden(partner):
                self.add(self.block_for(partner), partner)
        for child in [*msg.children, *(c for p in partners for c in p.children)]:
            self.walk(child)

    def block_for(self, msg: TemplateMessage) -> Optional[Block]:
        c = msg.content
        if isinstance(c, SessionHeaderMessage):
            # The screen's top line names the session; only a branch inside
            # it is worth a marker.
            if c.is_branch:
                return Block(kind="branch", title=c.preview or c.title)
            return None
        if isinstance(c, (UserSlashCommandMessage, UserMemoryMessage)):
            # The expanded body of a slash command (isMeta) and memory
            # notes: Claude Code shows the command, not what it expands to.
            return None
        if isinstance(c, UserTextMessage):
            text = _text_of(c.items)
            if not text:
                return None
            if text.startswith(_INTERRUPTED):
                return Block(kind="interrupted", title="Interrupted by user")
            return Block(kind="user", body=text, preview=10**9)
        if isinstance(c, SlashCommandMessage):
            title = f"/{c.command_name.lstrip('/')} {c.command_args}".strip()
            return Block(kind="user", body=title, preview=10**9)
        if isinstance(c, CommandOutputMessage):
            text = _plain(c.stdout).strip()
            return Block(kind="note", body=text) if text else None
        if isinstance(c, BashInputMessage):
            return Block(kind="user", body=f"! {c.command}", preview=10**9)
        if isinstance(c, BashOutputMessage):
            text = _plain("\n".join(t for t in (c.stdout, c.stderr) if t)).strip()
            return Block(kind="note", body=text) if text else None
        if isinstance(c, AssistantTextMessage):
            text = _text_of(c.items)
            if not text:
                return None
            return Block(kind="assistant", body=text, markdown=True, preview=10**9)
        if isinstance(c, ThinkingMessage):
            return Block(kind="thinking", title="Thinking…", body=c.thinking.strip())
        if isinstance(c, CompactedSummaryMessage):
            return Block(
                kind="compact",
                title="Conversation compacted",
                body=c.summary_text.strip(),
                markdown=True,
            )
        if isinstance(c, SystemMessage):
            if c.compact_trigger is not None or c.compact_pre_tokens is not None:
                return None  # the summary that follows says it
            if c.level == "info":
                return None  # hook chatter Claude Code does not keep
            text = _plain(c.text).strip()
            return Block(kind="note", body=text, error=c.level == "error")
        if isinstance(c, ToolResultMessage):
            # A result whose call was filtered out (or never paired).
            text = _result_text(c.output) or _clean_markdown(self.markdown_of(msg))
            return Block(kind="note", body=text, error=c.is_error) if text else None
        text = _clean_markdown(self.markdown_of(msg))
        return Block(kind="note", body=text) if text else None


def build_blocks(roots: list[TemplateMessage], ctx: RenderingContext) -> list[Block]:
    """Flatten upstream's message tree into the chat screen's blocks."""
    builder = _Builder(ctx)
    for root in roots:
        builder.walk(root)
    return builder.blocks


def load_blocks(
    project_path: Path,
    session_id: str,
    cache_manager: CacheManager,
    refresh: bool = True,
) -> list[Block]:
    """The session's blocks, from the cache (refreshed first unless archived)."""
    from .converter import ensure_fresh_cache
    from .renderer import generate_template_messages

    if refresh:
        ensure_fresh_cache(project_path, cache_manager, silent=True)
    entries: list[TranscriptEntry] = cache_manager.load_session_entries(session_id)
    if not entries:
        return []
    roots, _nav, ctx = generate_template_messages(entries)
    return build_blocks(roots, ctx)


# -- search ---------------------------------------------------------------


def _fold(text: str) -> str:
    from .tui_search import fold

    return fold(text)


def find_matches(blocks: list[Block], query: str) -> list[int]:
    """Indexes of the blocks containing every word of ``query``."""
    terms = _fold(query).split()
    if not terms:
        return []
    return [
        i for i, block in enumerate(blocks) if all(t in block.folded() for t in terms)
    ]


# -- drawing --------------------------------------------------------------


@dataclass
class Palette:
    """Theme colours as Rich styles, read once on the UI thread."""

    tool_ok: str = "green"
    tool_error: str = "red"
    current: str = "yellow"
    user_background: str = "on grey15"


@dataclass
class BlockState:
    """What changes how a block draws, besides the block itself."""

    expanded: bool = False
    terms: tuple[str, ...] = ()
    current: bool = False


def _gutter(mark: str, body: RenderableType, mark_style: str = "") -> Table:
    grid = Table.grid(padding=0, expand=True)
    grid.add_column(width=len(mark), no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(mark, style=mark_style), body)
    return grid


def _text(text: str, terms: tuple[str, ...], style: str = "") -> Text:
    out = Text(text, style=style)
    if terms:
        from .tui_search import HIGHLIGHT_STYLE, highlighted_folded

        for span in highlighted_folded(text, list(terms)).spans:
            out.stylize(HIGHLIGHT_STYLE, span.start, span.end)
    return out


def _prose(text: str, terms: tuple[str, ...], style: str = "") -> RenderableType:
    # Markdown cannot carry search highlights; plain text can.
    if terms:
        return _text(text, terms, style)
    return Markdown(text, style=style or "none")


def _lines(b: Block, state: BlockState, style: str = "dim") -> Text:
    lines = b.body.splitlines()
    shown = lines if state.expanded else lines[: b.preview]
    out = Text()
    for i, line in enumerate(shown):
        if i:
            out.append("\n")
        line_style = style
        if b.diff and i > 0:
            if line.startswith("+"):
                line_style = "green"
            elif line.startswith("-"):
                line_style = "red"
        if b.error:
            line_style = "red"
        out.append_text(_text(line, state.terms, line_style))
    hidden = len(lines) - len(shown)
    if hidden > 0:
        out.append(f"\n… +{hidden} lines (click to expand)", style="dim italic")
    return out


def block_renderable(b: Block, state: BlockState, palette: Palette) -> RenderableType:
    """The Rich renderable for one block, Claude Code's look."""
    terms = state.terms
    if b.kind == "user":
        return _gutter("> ", _text(b.body, terms), "bold")
    if b.kind == "assistant":
        return _gutter("● ", _prose(b.body, terms))
    if b.kind == "tool":
        name, _, rest = b.title.partition("(")
        head = _text(name, terms, "bold")
        if rest:
            head.append_text(_text("(" + rest, terms))
        dot = palette.tool_error if b.error else palette.tool_ok
        parts: list[RenderableType] = [_gutter("● ", head, dot)]
        if b.body:
            parts.append(_gutter("  ⎿  ", _lines(b, state), "dim"))
        return Group(*parts)
    if b.kind == "thinking":
        title = _text(f"✻ {b.title}", terms, "dim italic")
        if not state.expanded:
            return title
        return Group(title, _gutter("  ", _text(b.body, terms, "dim italic")))
    if b.kind == "compact":
        hint = "" if state.expanded else "  (click for summary)"
        title = _text(f"✻ {b.title}{hint}", terms, "dim")
        if not state.expanded:
            return title
        return Group(title, _gutter("  ⎿  ", _prose(b.body, terms, "dim")))
    if b.kind == "interrupted":
        return _gutter("  ⎿  ", _text(b.title, terms, "red"), "dim")
    if b.kind == "branch":
        return _text(f"⎇ Branch · {b.title}", terms, "dim italic")
    return _gutter("  ⎿  ", _lines(b, state), "dim")


class StripRenderer:
    """Renders blocks to terminal lines; safe to use off the UI thread."""

    def __init__(self, width: int, palette: Palette) -> None:
        from rich.console import Console

        self.width = max(20, width)
        self.palette = palette
        self.console = Console(
            width=self.width,
            color_system="truecolor",
            force_terminal=True,
            legacy_windows=False,
            no_color=False,
        )

    def render(self, b: Block, state: BlockState) -> list[Strip]:
        from rich.segment import Segment
        from rich.style import Style

        # One column of gutter on the left: the current find match's bar.
        inner = self.width - 2
        options = self.console.options.update_width(inner)
        lines = self.console.render_lines(
            block_renderable(b, state, self.palette), options, pad=True
        )
        bar = (
            Segment("▌", Style.parse(self.palette.current))
            if state.current
            else Segment(" ")
        )
        background = (
            Style.parse(self.palette.user_background) if b.kind == "user" else None
        )
        strips: list[Strip] = []
        for line in lines:
            segments = [bar, *line, Segment(" ")]
            if background is not None:
                segments = list(Segment.apply_style(segments, post_style=background))
            strips.append(Strip(segments, self.width))
        strips.append(Strip.blank(self.width))  # the gap Claude Code leaves
        return strips


class ConversationView(ScrollView, can_focus=True):
    """The conversation as one scrolling widget that draws only what shows.

    A long session is thousands of blocks; one widget each made Textual
    lay them all out on every change (seconds). Here each block is turned
    into lines once (off the UI thread for the bulk, see
    ``ConversationScreen``), and ``render_line`` just picks the visible ones.
    """

    DEFAULT_CSS = """
    ConversationView { height: 1fr; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.blocks: list[Block] = []
        self.states: list[BlockState] = []
        #: Rendered lines per block; None until rendered.
        self.strips: list[Optional[list[Strip]]] = []
        #: The contiguous range of rendered blocks [lo, hi).
        self.lo = 0
        self.hi = 0
        self._tops: list[int] = []  # first line of each block in [lo, hi)
        self.renderer: Optional[StripRenderer] = None

    # -- content ----------------------------------------------------------

    def set_blocks(self, blocks: list[Block], renderer: StripRenderer) -> None:
        self.blocks = blocks
        self.states = [BlockState() for _ in blocks]
        self.strips = [None] * len(blocks)
        self.lo = self.hi = 0
        self.renderer = renderer
        self._relayout()

    def add_rendered(self, start: int, rendered: list[list[Strip]]) -> None:
        """Take lines rendered off-thread for blocks ``start…``; extend the
        rendered range and keep what is on screen where it is."""
        end = start + len(rendered)
        for i, strips in enumerate(rendered, start):
            self.strips[i] = strips
        added_above = 0
        if self.hi == self.lo:  # first batch
            self.lo, self.hi = start, end
        elif end == self.lo:
            added_above = sum(len(s) for s in rendered)
            self.lo = start
        elif start == self.hi:
            self.hi = end
        self._relayout()
        if added_above:
            self.scroll_to(y=self.scroll_y + added_above, animate=False, immediate=True)

    def rerender(self, index: int) -> None:
        """Redraw one block after its state changed (click, find)."""
        if self.renderer is None or not (self.lo <= index < self.hi):
            return
        old = len(self.strips[index] or [])
        top = self._tops[index - self.lo]
        self.strips[index] = self.renderer.render(
            self.blocks[index], self.states[index]
        )
        grew = len(self.strips[index] or []) - old
        self._relayout()
        # A block above the view changing size must not move the view.
        if grew and top < self.scroll_y:
            self.scroll_to(y=self.scroll_y + grew, animate=False, immediate=True)

    def _relayout(self) -> None:
        tops: list[int] = []
        y = 0
        for i in range(self.lo, self.hi):
            tops.append(y)
            y += len(self.strips[i] or [])
        self._tops = tops
        width = self.renderer.width if self.renderer else self.size.width
        self.virtual_size = Size(width, y)
        self.refresh()

    # -- geometry ---------------------------------------------------------

    def block_at(self, line: int) -> Optional[int]:
        if not self._tops or line < 0:
            return None
        k = bisect_right(self._tops, line) - 1
        index = self.lo + k
        if index >= self.hi or line >= self._tops[k] + len(self.strips[index] or []):
            return None
        return index

    def top_of(self, index: int) -> Optional[int]:
        if self.lo <= index < self.hi:
            return self._tops[index - self.lo]
        return None

    def is_rendered(self, index: int) -> bool:
        return self.lo <= index < self.hi

    def scroll_to_block(self, index: int) -> None:
        top = self.top_of(index)
        if top is not None:
            self.scroll_to(
                y=max(0, top - self.size.height // 3), animate=False, immediate=True
            )

    # -- drawing ----------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        line = int(self.scroll_offset.y) + y
        width = self.size.width
        index = self.block_at(line)
        if index is None:
            return Strip.blank(width, self.rich_style)
        strips = self.strips[index] or []
        strip = strips[line - self._tops[index - self.lo]]
        return strip.crop_extend(0, width, self.rich_style)

    def on_click(self, event: Any) -> None:
        index = self.block_at(int(self.scroll_offset.y) + int(event.y))
        if index is not None and self.blocks[index].collapsible:
            state = self.states[index]
            state.expanded = not state.expanded
            self.rerender(index)


class ConversationScreen(ModalScreen[None]):
    """A session, full screen, drawn like Claude Code's chat.

    Modal so the list's single-letter keys (archive, delete, …) are off
    while reading: Textual stops the binding chain at a modal screen.

    The blocks around where it opens (the end, or the message it was opened
    on) are rendered first so the screen shows at once; the rest follow
    from a worker thread, the ones above with the view kept in place.
    """

    CSS = """
    ConversationScreen { layout: vertical; background: $background; }
    #conv-title {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-style: bold;
    }
    #conv-status { height: 1; padding: 0 2; color: $text-muted; }
    #conversation { height: 1fr; }
    #find-bar { height: 3; display: none; }
    #find-bar.-open { display: block; }
    #find-input {
        width: 1fr;
        background: transparent;
        border: round $primary;
        padding: 0 1;
    }
    #find-status { width: auto; padding: 1 1; color: $text-muted; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        # priority: the find box's Input binds ctrl+f itself.
        Binding("ctrl+f", "open_find", "Find", priority=True),
        Binding("ctrl+o", "toggle_all", "Expand/collapse all"),
        Binding("n", "find_next", "Next match", show=False),
        Binding("N", "find_prev", "Previous match", show=False),
    ]

    #: Blocks rendered around the opening point before the screen shows.
    FIRST_WINDOW = 120
    #: Blocks rendered per batch afterwards.
    CHUNK = 300
    #: Delay between the last keystroke in the find box and the search.
    FIND_DEBOUNCE = 0.2

    def __init__(
        self,
        project_path: Path,
        session_id: str,
        cache_manager: Optional[CacheManager] = None,
        *,
        project_label: str = "",
        refresh: bool = True,
        target_uuid: Optional[str] = None,
        find: str = "",
    ) -> None:
        super().__init__()
        self.project_path = project_path
        self.session_id = session_id
        self.cache_manager = cache_manager
        self.refresh_cache = refresh
        self.target_uuid = target_uuid
        self.initial_find = find
        self.project_label = project_label
        # The real name comes from the cache once the session loads.
        self.title_text = format_session_title(
            f"Session {session_id[:8]}", None, project_label
        )
        self.blocks: list[Block] = []
        self.loaded = False
        self.matches: list[int] = []
        self.current: int = -1  # index into matches
        self.all_expanded = False
        self._find_timer: Optional[Timer] = None
        # The block a match opened, closed again when the match moves on.
        self._opened_by_find: Optional[int] = None
        self._palette = Palette()

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, id="conv-title")
        yield Static("Loading…", id="conv-status")
        yield ConversationView(id="conversation")
        with Horizontal(id="find-bar"):
            yield Input(placeholder="Find in conversation…", id="find-input")
            yield Static("", id="find-status")
        yield Footer()

    @property
    def view(self) -> ConversationView:
        return self.query_one(ConversationView)

    def on_mount(self) -> None:
        self._palette = self._read_palette()
        self.view.focus()
        self._load(self._content_width())

    def _read_palette(self) -> Palette:
        from textual.color import Color

        from .tui_search import _app_of  # pyright: ignore[reportPrivateUsage]

        variables = _app_of(self).get_css_variables()

        def colour(name: str, fallback: str) -> str:
            try:
                return Color.parse(variables[name]).hex6
            except Exception:  # noqa: BLE001 - a missing variable keeps the default
                return fallback

        return Palette(
            tool_ok=colour("success", "green"),
            tool_error=colour("error", "red"),
            current=colour("accent", "yellow"),
            user_background="on " + colour("panel", "grey15"),
        )

    def _content_width(self) -> int:
        # The vertical scrollbar takes two columns.
        return max(20, self.size.width - 2)

    def on_resize(self) -> None:
        view = self.view
        if view.renderer is not None and view.renderer.width != self._content_width():
            self._rerender_all()

    # -- loading ----------------------------------------------------------

    @work(thread=True, exclusive=True, group="conversation")
    def _load(self, width: int) -> None:
        from .tui_search import _app_of  # pyright: ignore[reportPrivateUsage]

        app = _app_of(self)
        worker = _current_worker()
        try:
            manager = self.cache_manager
            if manager is None:
                from .cache import CacheManager, get_library_version

                manager = CacheManager(self.project_path, get_library_version())
            blocks = load_blocks(
                self.project_path, self.session_id, manager, refresh=self.refresh_cache
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            app.call_from_thread(self._status, f"Could not load the session: {e}")
            return
        app.call_from_thread(self._set_title, self._title_from(manager))
        if not blocks:
            app.call_from_thread(self._status, "This session has no messages to show.")
            return
        target = None
        if self.target_uuid is not None:
            target = next(
                (i for i, b in enumerate(blocks) if self.target_uuid in b.uuids), None
            )
        renderer = StripRenderer(width, self._palette)
        app.call_from_thread(self._start, blocks, renderer)
        self._render_in_batches(blocks, renderer, target, worker)

    def _title_from(self, manager: CacheManager) -> str:
        """The name the session list shows (AI title, summary or first
        prompt) and when it started, from the cache."""
        from .tui_search import session_name

        name = ""
        started: Optional[str] = None
        try:
            project = manager.get_cached_project_data()
            data = project.sessions.get(self.session_id) if project else None
        except Exception:  # noqa: BLE001 - a title is not worth an error
            data = None
        if data is not None:
            name = session_name(data.ai_title, data.summary, data.first_user_message)
            started = data.first_timestamp
        return format_session_title(
            name or f"Session {self.session_id[:8]}", started, self.project_label
        )

    def _set_title(self, title: str) -> None:
        self.title_text = title
        self.query_one("#conv-title", Static).update(title)

    def _render_in_batches(
        self,
        blocks: list[Block],
        renderer: StripRenderer,
        target: Optional[int],
        worker: Worker[None],
        states: Optional[list[BlockState]] = None,
    ) -> None:
        """Render every block off the UI thread: the opening window first,
        then the rest below, then the rest above."""
        from .tui_search import _app_of  # pyright: ignore[reportPrivateUsage]

        app = _app_of(self)
        n = len(blocks)
        st = states or [BlockState() for _ in blocks]
        centre = target if target is not None else n - 1
        lo = max(0, min(centre - self.FIRST_WINDOW // 2, n - self.FIRST_WINDOW))
        hi = min(n, lo + self.FIRST_WINDOW)

        def batch(a: int, b: int) -> list[list[Strip]]:
            return [renderer.render(blocks[i], st[i]) for i in range(a, b)]

        app.call_from_thread(self._first_batch, lo, batch(lo, hi), target)
        while hi < n:
            if worker.is_cancelled:
                return
            nxt = min(n, hi + self.CHUNK)
            app.call_from_thread(self._add_batch, hi, batch(hi, nxt))
            hi = nxt
        while lo > 0:
            if worker.is_cancelled:
                return
            prv = max(0, lo - self.CHUNK)
            app.call_from_thread(self._add_batch, prv, batch(prv, lo))
            lo = prv
        app.call_from_thread(self._done)

    def _add_batch(self, start: int, rendered: list[list[Strip]]) -> None:
        # Runs on the UI thread (the worker must not query the DOM).
        self.view.add_rendered(start, rendered)

    def _status(self, text: str) -> None:
        status = self.query_one("#conv-status", Static)
        status.display = bool(text)
        status.update(text)

    def _start(self, blocks: list[Block], renderer: StripRenderer) -> None:
        self.blocks = blocks
        self.view.set_blocks(blocks, renderer)

    def _first_batch(
        self, lo: int, rendered: list[list[Strip]], target: Optional[int]
    ) -> None:
        view = self.view
        view.add_rendered(lo, rendered)
        self._status("Loading the rest…" if len(rendered) < len(self.blocks) else "")
        if self.target_uuid is not None and target is None:
            self.notify("That message is not shown here (it may be a sub-agent's)")
        if target is not None:
            view.scroll_to_block(target)
        else:
            view.scroll_end(animate=False, immediate=True)
        if self.initial_find:
            self._open_find_with(self.initial_find, target)

    def _done(self) -> None:
        self.loaded = True
        self._status("")
        # A find typed while loading may point at blocks now drawn.
        if 0 <= self.current < len(self.matches):
            self.view.scroll_to_block(self.matches[self.current])

    def _rerender_all(self) -> None:
        """Width or expand-all changed: render everything again, keeping
        the block at the top of the view in place."""
        view = self.view
        if not self.blocks:
            return
        anchor = view.block_at(int(view.scroll_y))
        renderer = StripRenderer(self._content_width(), self._palette)
        states = view.states
        view.renderer = renderer
        self._rerender(renderer, states, anchor)

    @work(thread=True, exclusive=True, group="conversation")
    def _rerender(
        self,
        renderer: StripRenderer,
        states: list[BlockState],
        anchor: Optional[int],
    ) -> None:
        from .tui_search import _app_of  # pyright: ignore[reportPrivateUsage]

        worker = _current_worker()
        rendered = [renderer.render(b, s) for b, s in zip(self.blocks, states)]
        if worker.is_cancelled:
            return
        _app_of(self).call_from_thread(self._replace_all, rendered, anchor)

    def _replace_all(self, rendered: list[list[Strip]], anchor: Optional[int]) -> None:
        view = self.view
        view.strips = list(rendered)
        view.lo, view.hi = 0, len(rendered)
        view._relayout()  # pyright: ignore[reportPrivateUsage]
        if anchor is not None and (top := view.top_of(anchor)) is not None:
            view.scroll_to(y=top, animate=False, immediate=True)
        self._done()

    # -- actions ----------------------------------------------------------

    def action_close(self) -> None:
        bar = self.query_one("#find-bar")
        if bar.has_class("-open"):
            self._close_find()
            return
        from .tui_search import _app_of  # pyright: ignore[reportPrivateUsage]

        _app_of(self).pop_screen()

    def action_toggle_all(self) -> None:
        if not self.loaded:
            self.notify("Still loading…")
            return
        self.all_expanded = not self.all_expanded
        for state in self.view.states:
            state.expanded = self.all_expanded
        self._opened_by_find = None
        self._rerender_all()

    # -- find -------------------------------------------------------------

    def action_open_find(self) -> None:
        self.query_one("#find-bar").add_class("-open")
        self.query_one("#find-input", Input).focus()

    def _open_find_with(self, query: str, near: Optional[int]) -> None:
        self.action_open_find()
        find = self.query_one("#find-input", Input)
        with find.prevent(Input.Changed):
            find.value = query
        self._apply_find(query, near=near)

    def _close_find(self) -> None:
        self.query_one("#find-bar").remove_class("-open")
        find = self.query_one("#find-input", Input)
        with find.prevent(Input.Changed):
            find.value = ""
        self._apply_find("")
        self.view.focus()

    @on(Input.Changed, "#find-input")
    def _on_find_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._find_timer is not None:
            self._find_timer.stop()
        query = event.value
        self._find_timer = self.set_timer(
            self.FIND_DEBOUNCE, lambda: self._apply_find(query)
        )

    @on(Input.Submitted, "#find-input")
    def _on_find_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._find_timer is not None:
            # Enter before the debounce fired: search now.
            self._find_timer.stop()
            self._apply_find(event.value)
            return
        self.action_find_next()

    def on_key(self, event: Any) -> None:
        # Up/Down in the find box step through matches, like a pager.
        if not isinstance(self.focused, Input) or self.focused.id != "find-input":
            return
        if event.key == "down":
            event.stop()
            self.action_find_next()
        elif event.key == "up":
            event.stop()
            self.action_find_prev()

    def _apply_find(self, query: str, near: Optional[int] = None) -> None:
        self._find_timer = None
        self._set_current(-1)
        self.matches = find_matches(self.blocks, query)
        if not self.matches:
            self._find_status("no matches" if query.strip() else "")
            return
        if near is None:
            # Start from where we are reading.
            near = self.view.block_at(int(self.view.scroll_y))
        start = 0
        if near is not None:
            start = next((k for k, i in enumerate(self.matches) if i >= near), 0)
        self._go(start)

    def _find_status(self, text: str) -> None:
        self.query_one("#find-status", Static).update(text)

    def _set_current(self, current: int) -> None:
        view = self.view
        if 0 <= self.current < len(self.matches):
            index = self.matches[self.current]
            state = view.states[index]
            state.current, state.terms = False, ()
            if self._opened_by_find == index:
                state.expanded = False
            view.rerender(index)
        self._opened_by_find = None
        self.current = current
        if 0 <= current < len(self.matches):
            from .tui_search import title_terms

            index = self.matches[current]
            state = view.states[index]
            state.current = True
            state.terms = tuple(title_terms(self.query_one("#find-input", Input).value))
            if self.blocks[index].collapsible and not state.expanded:
                # A match inside a collapsed result is useless unless shown.
                state.expanded = True
                self._opened_by_find = index
            view.rerender(index)

    def _go(self, current: int) -> None:
        if not self.matches:
            return
        current %= len(self.matches)
        self._set_current(current)
        index = self.matches[current]
        status = f"{current + 1}/{len(self.matches)}"
        if not self.view.is_rendered(index):
            status += "  (loading that part…)"
        self._find_status(status)
        self.view.scroll_to_block(index)

    def action_find_next(self) -> None:
        self._go(self.current + 1)

    def action_find_prev(self) -> None:
        self._go(self.current - 1)


def format_session_title(
    title: str, first_timestamp: Optional[str], project_label: str = ""
) -> str:
    """``Title · 2026-11-09 14:03 · ~/projects/x`` for the screen's top line."""
    parts = [title]
    if first_timestamp:
        try:
            when = datetime.fromisoformat(first_timestamp.replace("Z", "+00:00"))
            parts.append(when.astimezone().strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass
    if project_label:
        parts.append(project_label)
    return "  ·  ".join(parts)
