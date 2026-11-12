"""session-lens: full-text search inside the TUI.

Kept in its own module so the upstream ``tui.py`` only grows a key binding
and a one-line action per app, which keeps merges from upstream cheap.

The search itself is upstream's FTS5 index (``search.py``) — the same one
``serve`` uses. This module only makes sure the cache and the index are
current, runs queries off the UI thread and shows the hits.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional, cast
from urllib.parse import quote

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Input, Static
from textual.worker import Worker
from textual.worker import get_current_worker  # pyright: ignore[reportUnknownVariableType]

from . import search as fts
from .cache import (
    CacheManager,
    checkpoint_wal,
    get_cache_db_path,
    get_library_version,
)
from .converter import ensure_fresh_cache, generate_single_session_file
from .migrations.runner import apply_write_pragmas, cache_busy_timeout
from .tui_viewer import ConversationScreen
from .utils import DEFAULT_THEME, best_working_dir

#: Rows fetched per query. FTS5 ranks by relevance, so the first page is the
#: useful one; more than this is a sign the query should be narrowed.
RESULT_LIMIT = 200

#: Delay between the last keystroke and the query (search-as-you-type).
DEBOUNCE_SECONDS = 0.25

HIGHLIGHT_STYLE = "bold reverse"

#: The bar on the list screens: one line, so it says only what it does.
BAR_PLACEHOLDER = "🔎 Search sessions…  (Ctrl+F, / or click)"

TITLE_PLACEHOLDER = "🔎 Search session names…  (Ctrl+R: search message content)"
FULL_TEXT_PLACEHOLDER = (
    '🔎 Search messages…  ("exact phrase" in quotes · word* for prefix'
    " · Ctrl+R: search session names)"
)

#: The ``type`` shown for a session-name hit (it is a session, not a message).
TITLE_HIT_TYPE = "session"

# Cache refresh and index build each run once per process per cache file.
# The project selector and the session browser are separate apps run in turn
# by the CLI, so this has to live at module level, not on a screen. They are
# tracked apart because a session-name search needs only the cache: the FTS
# index (the slow first build) waits until a full-text search asks for it.
_cache_prepared: set[str] = set()
_index_prepared: set[str] = set()
_prepare_lock = threading.Lock()


def _app_of(node: Any) -> "App[Any]":
    """``node.app`` typed: Textual declares it as ``App[Unknown]``."""
    return node.app  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _current_worker() -> "Worker[None]":
    return cast("Worker[None]", get_current_worker())


def _terms(query: str) -> list[str]:
    """The query's plain words, for highlighting (upstream's helper)."""
    return fts._plain_terms(query)  # pyright: ignore[reportPrivateUsage]


class PrepareError(Exception):
    """Search can't run on this archive; the message is shown to the user."""


def _active_projects(projects_dir: Path) -> list[Path]:
    return sorted(
        d for d in projects_dir.iterdir() if d.is_dir() and any(d.glob("*.jsonl"))
    )


def prepare_search(
    projects_dir: Path,
    progress: Callable[[str], None],
    cancelled: Callable[[], bool],
    full_text: bool = False,
) -> Path:
    """Bring every project's cache, and for ``full_text`` the FTS index, up to date.

    The TUI only refreshes the cache of the project it opens, so a search
    over the archive would silently miss every project never opened.
    Refreshing is incremental (mtime-based), so after the first run this is
    a stat per file plus whatever changed.

    Returns the cache database path. Raises ``PrepareError``.
    """
    db_path = get_cache_db_path(projects_dir)
    key = str(db_path)
    with _prepare_lock:
        if key not in _cache_prepared:
            _refresh_caches(projects_dir, progress, cancelled)
            if not db_path.exists():
                raise PrepareError("Cache database not found.")
            _cache_prepared.add(key)
        if not full_text or key in _index_prepared:
            return db_path

        if not fts.fts5_available():
            raise PrepareError(
                "This SQLite build has no FTS5; full-text search is unavailable."
            )

        conn = sqlite3.connect(db_path, timeout=cache_busy_timeout())
        try:
            apply_write_pragmas(conn)

            def report(done: int, total: int) -> None:
                if total:
                    progress(f"Building search index… {done}/{total} files")

            status = fts.ensure_index(conn, progress=report)
        finally:
            conn.close()
        checkpoint_wal(db_path)

        if not status.ready:
            raise PrepareError(f"Search index not ready: {status.reason or '?'}")
        _index_prepared.add(key)
        return db_path


def _refresh_caches(
    projects_dir: Path,
    progress: Callable[[str], None],
    cancelled: Callable[[], bool],
) -> None:
    projects = _active_projects(projects_dir)
    version = get_library_version()
    for i, project in enumerate(projects, 1):
        if cancelled():
            raise PrepareError("Cancelled.")
        progress(f"Updating cache… {i}/{len(projects)}  {project.name}")
        try:
            ensure_fresh_cache(project, CacheManager(project, version), silent=True)
        except Exception:  # one broken project must not block search
            pass


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=cache_busy_timeout())


def run_search(
    db_path: Path, query: str, project_path: Optional[Path]
) -> list[fts.SearchResult]:
    conn = _connect_readonly(db_path)
    try:
        project_id: Optional[int] = None
        if project_path is not None:
            row = conn.execute(
                "SELECT id FROM projects WHERE project_path IN (?, ?)",
                (str(project_path), str(project_path.resolve())),
            ).fetchone()
            if row is None:
                return []
            project_id = int(row[0])
        return fts.search(conn, query, project_id=project_id, limit=RESULT_LIMIT)
    finally:
        conn.close()


def session_name(
    ai_title: Optional[str], summary: Optional[str], first_user_message: Optional[str]
) -> str:
    """The name the session list shows: AI title, then summary, then prompt."""
    name = ai_title or summary or first_user_message or ""
    return " ".join(name.split())


#: Turkish has two i's, and ``casefold`` pairs "I" with "i" (not "ı") and
#: turns "İ" into "i" plus a combining dot. Treat all four as one letter.
_I_VARIANTS = str.maketrans({"I": "i", "İ": "i", "ı": "i"})


def fold(text: str) -> str:
    """Case-insensitive form for name search, Turkish i's included."""
    return text.translate(_I_VARIANTS).casefold()


def title_terms(query: str) -> list[str]:
    """Words of a session-name query; quotes are allowed but mean nothing."""
    return [t for t in fold(query.replace('"', " ")).split() if t]


def highlighted_folded(text: str, terms: list[str]) -> Text:
    """Like ``highlighted``, but matching the way ``title_matches`` does.

    Rich's ``case_sensitive=False`` does not know Turkish i's, so a hit found
    by ``fold`` would show unhighlighted. Fold per character and map each
    match back to the original span (folding can change a character's length).
    """
    rich_text = Text(text)
    folded = ""
    origin: list[int] = []  # folded position -> index in text
    for i, ch in enumerate(text):
        piece = fold(ch)
        folded += piece
        origin += [i] * len(piece)
    for term in terms:
        start = folded.find(term)
        while start != -1:
            end = start + len(term)
            rich_text.stylize(HIGHLIGHT_STYLE, origin[start], origin[end - 1] + 1)
            start = folded.find(term, end)
    return rich_text


def title_matches(name: str, terms: list[str]) -> bool:
    """Every term appears in the name, ignoring case (Unicode-aware)."""
    folded = fold(name)
    return bool(terms) and all(t in folded for t in terms)


def run_title_search(
    db_path: Path, query: str, project_path: Optional[Path]
) -> list[fts.SearchResult]:
    """Sessions whose name contains every word of ``query``, newest first.

    Filtered in Python rather than with ``LIKE``: SQLite only folds ASCII
    case, and an archive has thousands of sessions, not millions.
    """
    terms = title_terms(query)
    if not terms:
        return []
    conn = _connect_readonly(db_path)
    try:
        sql = (
            "SELECT s.session_id, p.project_path, s.last_timestamp,"
            " s.ai_title, s.summary, s.first_user_message"
            " FROM sessions s JOIN projects p ON p.id = s.project_id"
            " WHERE s.hidden = 0"
        )
        params: list[str] = []
        if project_path is not None:
            sql += " AND p.project_path IN (?, ?)"
            params += [str(project_path), str(project_path.resolve())]
        sql += " ORDER BY s.last_timestamp DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    results: list[fts.SearchResult] = []
    for session_id, proj, last_ts, ai_title, summary, first_msg in rows:
        name = session_name(ai_title, summary, first_msg)
        if not title_matches(name, terms):
            continue
        results.append(
            fts.SearchResult(
                message_id=-1,
                project_path=proj,
                project_slug=fts.project_slug(proj),
                session_id=session_id,
                message_uuid=None,
                timestamp=last_ts or None,
                type=TITLE_HIT_TYPE,
                field="title",
                snippet=name,
                link="",
            )
        )
        if len(results) >= RESULT_LIMIT:
            break
    return results


def project_label(project_path: str) -> str:
    """``-home-me-projects-x`` -> ``~/projects/x``."""
    slug = fts.project_slug(project_path)
    name = best_working_dir(slug, None)[0]
    home = str(Path.home()).lstrip("/")
    if name == home:
        return "~"
    if name.startswith(home + "/"):
        return "~/" + name[len(home) + 1 :]
    return name


def format_timestamp(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        return (
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        return ts[:16]


def highlighted(text: str, terms: list[str]) -> Text:
    rich_text = Text(text)
    rich_text.highlight_words(
        [t for t in terms if t], style=HIGHLIGHT_STYLE, case_sensitive=False
    )
    return rich_text


def open_session_page(
    project_dir: Path,
    session_id: str,
    theme: str,
    message_uuid: Optional[str] = None,
    query: str = "",
) -> Path:
    """Render a session's HTML page if needed, open it, return its path."""
    page = project_dir / f"session-{session_id}.html"
    from .renderer import get_renderer

    if not page.exists() or get_renderer("html", theme=theme).is_outdated(page):
        page = generate_single_session_file(
            "html", project_dir, session_id, theme=theme
        )
    params: list[str] = []
    if message_uuid:
        params.append(f"uuid={quote(message_uuid)}")
    if query.strip():
        params.append(f"q={quote(query)}")
    url = page.as_uri() + ("?" + "&".join(params) if params else "")
    webbrowser.open(url)
    return page


def open_session_html(result: fts.SearchResult, query: str, theme: str) -> Path:
    """Render the hit's session page if needed; return the path opened."""
    if not result.session_id:
        raise ValueError("This result has no session")
    uuid = None
    if result.message_uuid and result.type not in fts.SESSION_ONLY_TYPES:
        uuid = result.message_uuid
    return open_session_page(
        Path(result.project_path), result.session_id, theme, uuid, query
    )


@dataclass
class SessionStats:
    """How big a session is, for the search results' columns."""

    messages: int = 0
    tokens: int = 0
    #: Bytes of the session's transcript file; None when it is gone.
    size: Optional[int] = None
    #: Directory the session ran in, for resuming it in Claude Code.
    cwd: str = ""
    #: The session's name, as the session list shows it.
    name: str = ""


def session_stats(
    db_path: Path, results: list[fts.SearchResult]
) -> dict[str, SessionStats]:
    """Message count, tokens and transcript size of each hit's session."""
    ids = sorted({r.session_id for r in results if r.session_id})
    stats: dict[str, SessionStats] = {}
    if not ids:
        return stats
    conn = _connect_readonly(db_path)
    try:
        for start in range(0, len(ids), 500):  # under SQLite's parameter cap
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                "SELECT session_id, message_count, total_input_tokens,"
                " total_output_tokens, cwd, ai_title, summary, first_user_message"
                f" FROM sessions WHERE session_id IN ({marks})",
                chunk,
            ):
                sid, messages, tin, tout, cwd, ai_title, summary, prompt = row
                stats[sid] = SessionStats(
                    messages=int(messages or 0),
                    tokens=int(tin or 0) + int(tout or 0),
                    cwd=cwd or "",
                    name=session_name(ai_title, summary, prompt),
                )
    finally:
        conn.close()
    for r in results:
        if r.session_id and r.session_id in stats and stats[r.session_id].size is None:
            try:
                path = Path(r.project_path) / f"{r.session_id}.jsonl"
                stats[r.session_id].size = path.stat().st_size
            except OSError:
                pass
    return stats


def format_size(size: Optional[int]) -> str:
    """``734 B``, ``12.4 KB``, ``3.1 MB``; ``-`` when unknown."""
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_count(value: int) -> str:
    """``950``, ``12.3k``, ``4.1M``: tokens in a narrow column."""
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


@dataclass
class SessionRow:
    """One session of the archive, as the all-projects list shows it."""

    session_id: str
    project_path: str
    name: str
    first_timestamp: str
    last_timestamp: str
    message_count: int
    tokens: int
    cwd: str


def all_sessions(db_path: Path) -> list[SessionRow]:
    """Every visible session in the cache, most recently active first."""
    conn = _connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT s.session_id, p.project_path, s.ai_title, s.summary,"
            " s.first_user_message, s.first_timestamp, s.last_timestamp,"
            " s.message_count, s.total_input_tokens, s.total_output_tokens, s.cwd"
            " FROM sessions s JOIN projects p ON p.id = s.project_id"
            " WHERE s.hidden = 0"
            " ORDER BY s.last_timestamp DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        SessionRow(
            session_id=sid,
            project_path=proj,
            name=session_name(ai_title, summary, first_msg),
            first_timestamp=first or "",
            last_timestamp=last or "",
            message_count=int(count or 0),
            tokens=int(tin or 0) + int(tout or 0),
            cwd=cwd or "",
        )
        for (
            sid,
            proj,
            ai_title,
            summary,
            first_msg,
            first,
            last,
            count,
            tin,
            tout,
            cwd,
        ) in rows
    ]


def resume_in_claude(app: App[Any], session_id: str, cwd: str = "") -> None:
    """Replace this process with ``claude -r``, from the session's directory."""
    if cwd:
        target = Path(cwd)
        if target.is_dir():
            os.chdir(target)
        else:
            app.notify(f"Session directory not found: {cwd}", severity="warning")
    try:
        with app.suspend():
            os.execvp("claude", ["claude", "-r", session_id])
    except FileNotFoundError:
        app.notify(
            "Claude Code CLI not found. Make sure 'claude' is in your PATH.",
            severity="error",
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the user
        app.notify(f"Error resuming session: {e}", severity="error")


def _open_html_in_background(
    screen: "Screen[Any]", result: fts.SearchResult, query: str, theme: str
) -> None:
    screen.notify("Preparing the session page…")

    def job() -> None:
        try:
            page = open_session_html(result, query, theme)
            _app_of(screen).call_from_thread(screen.notify, f"Opened: {page.name}")
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            _app_of(screen).call_from_thread(
                screen.notify, f"Could not open: {e}", severity="error"
            )

    threading.Thread(target=job, daemon=True).start()


class SearchScreen(ModalScreen[None]):
    """Search box + live result table over the whole archive or one project.

    Modal so the list's single-letter keys (archive, delete, …) are off
    while the results have focus: Textual stops the binding chain there.
    """

    CSS = """
    SearchScreen { layout: vertical; background: $background; }
    #search-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    /* Same box as the SearchBar on the list screens. */
    #search-input {
        height: 3;
        padding: 0 1;
        background: transparent;
        border: round $primary 60%;
    }
    #search-input:focus { border: round $primary; }
    #search-status { height: 1; padding: 0 1; color: $text-muted; }
    #search-results { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("down", "focus_results", "To results", show=False),
        Binding("ctrl+r", "toggle_mode", "Names/content"),
        # Same key as on the list screens. priority: the Input binds ctrl+f.
        Binding("ctrl+f", "focus_input", "Search box", show=False, priority=True),
        Binding("ctrl+t", "toggle_scope", "Project/all"),
        Binding("o", "open_html", "Open in browser"),
        Binding("c", "resume_selected", "Resume in Claude Code"),
        Binding("slash", "focus_input", "Back to search", show=False),
    ]

    def __init__(
        self,
        projects_dir: Path,
        project_path: Optional[Path] = None,
        theme: str = DEFAULT_THEME,
        initial_query: str = "",
    ) -> None:
        super().__init__()
        self.projects_dir = projects_dir
        self.project_path = project_path
        self.scoped = project_path is not None
        #: False: session names only (the default, needs only the cache).
        #: True: every message's text through the FTS index.
        self.full_text = False
        self.html_theme = theme
        self.db_path: Optional[Path] = None
        self._index_ready = False
        self._shown_full_text = False
        self.results: list[fts.SearchResult] = []
        self.stats: dict[str, SessionStats] = {}
        self._query = initial_query
        self._debounce: Optional[Timer] = None
        # Enter in the box means "take me to the results"; they arrive from a
        # worker, so the move happens in _show().
        self._focus_results = False

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Same place as the SearchBar on the list screens, so going from the
        # bar to this screen looks like the results opening under the bar.
        yield Input(
            value=self._query,
            placeholder=self._placeholder(),
            id="search-input",
        )
        yield Static(self._title(), id="search-title")
        yield Static("Preparing…", id="search-status")
        table: DataTable[Text | str] = DataTable(
            id="search-results", cursor_type="row", zebra_stripes=True
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._add_columns()
        search_input = self.query_one(Input)
        search_input.focus()
        search_input.cursor_position = len(self._query)
        self._prepare()

    #: Fixed columns; the match column gets the rest. A name hit's match is
    #: the session's name, so its row names the project; a message hit's
    #: match is a line of the transcript, so its row names the session.
    _NAME_COLUMNS = (
        ("Date", 16),
        ("Project", 22),
        ("Type", 8),
        ("Msgs", 6),
        ("Tokens", 7),
        ("Size", 8),
    )
    _CONTENT_COLUMNS = (
        ("Date", 16),
        ("Session", 32),
        ("Type", 8),
        ("Msgs", 6),
        ("Tokens", 7),
        ("Size", 8),
    )

    def _fixed_columns(self) -> tuple[tuple[str, int], ...]:
        return self._CONTENT_COLUMNS if self._shown_full_text else self._NAME_COLUMNS

    def _match_width(self) -> int:
        # Each column carries one cell of padding per side; leave room for
        # the vertical scrollbar so a long result list never scrolls sideways.
        fixed = sum(w + 2 for _, w in self._fixed_columns())
        return max(20, self.size.width - fixed - 2 - 2)

    def _add_columns(self) -> None:
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        table.clear(columns=True)
        for label, width in self._fixed_columns():
            table.add_column(label, width=width)
        table.add_column("Match", width=self._match_width())

    def on_resize(self) -> None:
        if not self.is_mounted:
            return
        row = cast("DataTable[Text | str]", self.query_one(DataTable)).cursor_row
        self._add_columns()
        self._fill_rows(self._shown_full_text)
        if self.results:
            table = cast("DataTable[Text | str]", self.query_one(DataTable))
            table.move_cursor(row=min(row, len(self.results) - 1))

    def _placeholder(self) -> str:
        return FULL_TEXT_PLACEHOLDER if self.full_text else TITLE_PLACEHOLDER

    def _title(self) -> str:
        if self.scoped and self.project_path is not None:
            scope = f"project: {project_label(str(self.project_path))}"
        else:
            scope = "all projects"
        mode = "message content" if self.full_text else "session names"
        return f"🔎 Search — {mode} — {scope}"

    def _status(self, text: str) -> None:
        self.query_one("#search-status", Static).update(text)

    # -- preparation ------------------------------------------------------

    @work(thread=True, exclusive=True, group="prepare")
    def _prepare(self, full_text: bool = False) -> None:
        worker = _current_worker()

        def progress(msg: str) -> None:
            _app_of(self).call_from_thread(self._status, msg)

        try:
            db_path = prepare_search(
                self.projects_dir, progress, lambda: worker.is_cancelled, full_text
            )
        except PrepareError as e:
            _app_of(self).call_from_thread(self._status, f"Search unavailable: {e}")
            return
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            _app_of(self).call_from_thread(self._status, f"Error: {e}")
            return
        _app_of(self).call_from_thread(self._ready, db_path, full_text)

    def _ready(self, db_path: Path, full_text: bool = False) -> None:
        self.db_path = db_path
        self._index_ready = self._index_ready or full_text
        if self._query:
            self._search(self._query)
        else:
            self._status("Ready. Start typing.")

    # -- searching --------------------------------------------------------

    @on(Input.Changed, "#search-input")
    def _on_change(self, event: Input.Changed) -> None:
        event.stop()
        self._query = event.value
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(
            DEBOUNCE_SECONDS, lambda: self._search(self._query)
        )

    @on(Input.Submitted, "#search-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        if self._debounce is not None:
            self._debounce.stop()
        self._query = event.value
        self._focus_results = True
        self._search(self._query)

    def _search(self, query: str) -> None:
        if self.db_path is None:
            return  # _ready() runs the latest query once the cache is fresh
        if self.full_text and not self._index_ready:
            self._prepare(full_text=True)  # _ready() reruns the query
            return
        empty = not fts.fts_escape(query) if self.full_text else not title_terms(query)
        if empty:
            self.results = []
            cast("DataTable[Text | str]", self.query_one(DataTable)).clear()
            self._status("Ready. Start typing.")
            return
        self._run_query(
            self.db_path,
            query,
            self.project_path if self.scoped else None,
            self.full_text,
        )

    @work(thread=True, exclusive=True, group="search")
    def _run_query(
        self,
        db_path: Path,
        query: str,
        project_path: Optional[Path],
        full_text: bool,
    ) -> None:
        worker = _current_worker()
        try:
            if full_text:
                results = run_search(db_path, query, project_path)
            else:
                results = run_title_search(db_path, query, project_path)
            stats = session_stats(db_path, results)
        except sqlite3.Error as e:
            if not worker.is_cancelled:
                _app_of(self).call_from_thread(self._status, f"Query error: {e}")
            return
        if not worker.is_cancelled:
            _app_of(self).call_from_thread(self._show, query, results, full_text, stats)

    def _show(
        self,
        query: str,
        results: list[fts.SearchResult],
        full_text: bool,
        stats: Optional[dict[str, SessionStats]] = None,
    ) -> None:
        if query != self._query or full_text != self.full_text:
            return  # a newer query (or the other mode's) is on its way
        self.results = results
        self.stats = stats or {}
        self._fill_rows(full_text)
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        if self._focus_results and results:
            table.focus()
        self._focus_results = False
        if not results:
            self._status("No results.")
        else:
            more = "+" if len(results) >= RESULT_LIMIT else ""
            self._status(
                f"{len(results)}{more} results  ·  Enter: read"
                "  ·  o: open in browser  ·  c: resume in Claude Code"
                "  ·  Ctrl+F: back to search"
            )

    def _fill_rows(self, full_text: bool) -> None:
        if full_text != self._shown_full_text:
            self._shown_full_text = full_text
            self._add_columns()
        terms = _terms(self._query) if full_text else title_terms(self._query)
        mark = highlighted if full_text else highlighted_folded
        width = self._match_width()
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        table.clear()
        for i, r in enumerate(self.results):
            match = mark(r.snippet, terms)
            match.truncate(width, overflow="ellipsis")
            st = self.stats.get(r.session_id or "")
            if full_text:
                second = Text(st.name if st else "", no_wrap=True)
                second.truncate(self._CONTENT_COLUMNS[1][1], overflow="ellipsis")
            else:
                second = Text(project_label(r.project_path), no_wrap=True)
            table.add_row(
                format_timestamp(r.timestamp),
                second,
                r.type,
                Text(str(st.messages) if st else "-", justify="right"),
                Text(format_count(st.tokens) if st else "-", justify="right"),
                Text(format_size(st.size) if st else "-", justify="right"),
                match,
                key=str(i),
            )

    @on(DataTable.RowHighlighted, "#search-results")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()

    # -- actions ----------------------------------------------------------

    def _selected(self) -> Optional[fts.SearchResult]:
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        if not self.results or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self.results):
            return None
        return self.results[table.cursor_row]

    @on(DataTable.RowSelected, "#search-results")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # Both list apps handle RowSelected/RowHighlighted at app level
        # (selecting a project exits the selector!), so ours must not bubble.
        event.stop()
        result = self._selected()
        if result is None or not result.session_id:
            return
        # Read it in the TUI, as Enter does in the session list. A message
        # hit opens at that message with the query found; `o` still opens
        # the browser.
        is_message = result.type != TITLE_HIT_TYPE
        _app_of(self).push_screen(
            ConversationScreen(
                Path(result.project_path),
                result.session_id,
                project_label=project_label(result.project_path),
                # prepare_search() just refreshed every project's cache.
                refresh=False,
                target_uuid=result.message_uuid if is_message else None,
                find=self._query if is_message else "",
            )
        )

    def action_open_html(self) -> None:
        if isinstance(self.focused, Input):
            return
        result = self._selected()
        if result is None:
            self.notify("Select a result first", severity="warning")
            return
        _open_html_in_background(self, result, self._query, self.html_theme)

    def action_resume_selected(self) -> None:
        if isinstance(self.focused, Input):
            return
        result = self._selected()
        if result is None or not result.session_id:
            self.notify("Select a result first", severity="warning")
            return
        st = self.stats.get(result.session_id)
        resume_in_claude(_app_of(self), result.session_id, st.cwd if st else "")

    def action_toggle_mode(self) -> None:
        self.full_text = not self.full_text
        self.query_one("#search-title", Static).update(self._title())
        self.query_one(Input).placeholder = self._placeholder()
        self._search(self._query)

    def action_toggle_scope(self) -> None:
        if self.project_path is None:
            self.notify("Already searching all projects")
            return
        self.scoped = not self.scoped
        self.query_one("#search-title", Static).update(self._title())
        self._search(self._query)

    def action_focus_results(self) -> None:
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        if table.row_count:
            table.focus()

    def action_focus_input(self) -> None:
        self.query_one(Input).focus()

    def action_close(self) -> None:
        _app_of(self).pop_screen()


class SearchBar(Input):
    """Always-visible search box at the top of the list screens.

    It does not take focus on start: the list screens drive everything with
    single-letter keys, which a focused input would swallow. ``/`` focuses
    it; the first keystroke opens ``SearchScreen`` with the text carried
    over, and Esc hands focus back to the list.
    """

    DEFAULT_CSS = """
    /* A thin rounded box: one line of text, its frame drawn through the
       middle of the cells above and below, so it reads as about two lines.
       Transparent, so the frame is the box rather than a filled block.
       No margin: the frame's own half-cells already space it from the
       stats box above and the table below. No side margin either: a
       sibling's side margin narrows the 1fr table next to it. */
    SearchBar {
        height: 3;
        padding: 0 1;
        margin: 0;
        background: transparent;
        border: round $primary 60%;
    }
    SearchBar:focus { border: round $primary; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "leave", "Back to list", show=False),
    ]

    def __init__(
        self,
        projects_dir: Path,
        project_path: Optional[Path] = None,
        theme: str = DEFAULT_THEME,
    ) -> None:
        super().__init__(placeholder=BAR_PLACEHOLDER, id="session-lens-search-bar")
        self.projects_dir = projects_dir
        self.project_path = project_path
        self.html_theme = theme
        # Not focusable until "/" asks for it: the bar sits above the list,
        # so Textual's auto-focus would otherwise pick it on start and the
        # list's single-letter keys would be typed into it instead.
        self.can_focus = False

    def activate(self) -> None:
        self.can_focus = True
        self.focus()

    def deactivate(self) -> None:
        with self.prevent(Input.Changed):
            self.value = ""
        self.can_focus = False
        self.screen.focus_next()

    def on_blur(self) -> None:
        self.can_focus = False

    def on_click(self) -> None:
        # Not focusable until activated, so a click alone would not reach it.
        if not self.has_focus:
            self.activate()

    @on(Input.Changed)
    def _open_results(self, event: Input.Changed) -> None:
        event.stop()
        if event.value == "/":
            # The "/" that focused the bar can land in it on some terminals.
            with self.prevent(Input.Changed):
                self.value = ""
            return
        if not event.value.strip():
            return
        query = event.value
        # Leave the list focused underneath, so Esc on the results screen
        # lands back on the list rather than in this box.
        self.deactivate()
        _app_of(self).push_screen(
            SearchScreen(
                self.projects_dir,
                self.project_path,
                self.html_theme,
                initial_query=query,
            )
        )

    @on(Input.Submitted)
    def _swallow_submit(self, event: Input.Submitted) -> None:
        event.stop()

    def action_leave(self) -> None:
        self.deactivate()


def focus_search_bar(app: object) -> None:
    """``/`` handler shared by the list screens."""
    if isinstance(app, App):
        try:
            app.query_one(SearchBar).activate()
        except Exception:  # noqa: BLE001 - no bar on this screen
            pass
