"""session-lens: full-text search inside the TUI.

Kept in its own module so the upstream ``tui.py`` only grows a key binding
and a one-line action per app, which keeps merges from upstream cheap.

The search itself is upstream's FTS5 index (``search.py``) — the same one
``serve`` uses. This module only makes sure the cache and the index are
current, runs queries off the UI thread and shows the hits.
"""

from __future__ import annotations

import sqlite3
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
from textual.containers import Container, VerticalScroll
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
from .utils import DEFAULT_THEME, best_working_dir

#: Rows fetched per query. FTS5 ranks by relevance, so the first page is the
#: useful one; more than this is a sign the query should be narrowed.
RESULT_LIMIT = 200

#: Delay between the last keystroke and the query (search-as-you-type).
DEBOUNCE_SECONDS = 0.25

HIGHLIGHT_STYLE = "bold reverse"

SEARCH_PLACEHOLDER = (
    '🔎 Ara…  (/ ile odaklan · "tam ifade" için tırnak · kelime* ile önek)'
)

# Cache refresh + index build runs once per process per cache file. The
# project selector and the session browser are separate apps run in turn by
# the CLI, so this has to live at module level, not on a screen.
_prepared: set[str] = set()
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
) -> Path:
    """Bring every project's cache and the FTS index up to date.

    The TUI only refreshes the cache of the project it opens, so a search
    over the archive would silently miss every project never opened.
    Refreshing is incremental (mtime-based), so after the first run this is
    a stat per file plus whatever changed.

    Returns the cache database path. Raises ``PrepareError``.
    """
    db_path = get_cache_db_path(projects_dir)
    with _prepare_lock:
        if str(db_path) in _prepared:
            return db_path

        if not fts.fts5_available():
            raise PrepareError("Bu SQLite derlemesinde FTS5 yok; arama yapılamaz.")

        projects = _active_projects(projects_dir)
        version = get_library_version()
        failed: list[str] = []
        for i, project in enumerate(projects, 1):
            if cancelled():
                raise PrepareError("İptal edildi.")
            progress(f"Önbellek güncelleniyor… {i}/{len(projects)}  {project.name}")
            try:
                ensure_fresh_cache(project, CacheManager(project, version), silent=True)
            except Exception:  # one broken project must not block search
                failed.append(project.name)

        if not db_path.exists():
            raise PrepareError("Önbellek veritabanı bulunamadı.")

        conn = sqlite3.connect(db_path, timeout=cache_busy_timeout())
        try:
            apply_write_pragmas(conn)

            def report(done: int, total: int) -> None:
                if total:
                    progress(f"Arama indeksi kuruluyor… {done}/{total} dosya")

            status = fts.ensure_index(conn, progress=report)
        finally:
            conn.close()
        checkpoint_wal(db_path)

        if not status.ready:
            raise PrepareError(f"Arama indeksi hazır değil: {status.reason or '?'}")
        _prepared.add(str(db_path))
        if failed:
            progress(f"Uyarı: {len(failed)} proje önbelleğe alınamadı.")
        return db_path


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


def load_message_fields(db_path: Path, message_id: int) -> dict[str, str]:
    conn = _connect_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return fts.extract_search_text(fts.decode_entry(row[0]))


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


def open_session_html(result: fts.SearchResult, query: str, theme: str) -> Path:
    """Render the hit's session page if needed; return the path opened."""
    if not result.session_id:
        raise ValueError("Bu sonucun oturumu yok")
    project_dir = Path(result.project_path)
    page = project_dir / f"session-{result.session_id}.html"
    from .renderer import get_renderer

    if not page.exists() or get_renderer("html", theme=theme).is_outdated(page):
        page = generate_single_session_file(
            "html", project_dir, result.session_id, theme=theme
        )
    params: list[str] = []
    if result.message_uuid and result.type not in fts.SESSION_ONLY_TYPES:
        params.append(f"uuid={quote(result.message_uuid)}")
    if query.strip():
        params.append(f"q={quote(query)}")
    url = page.as_uri() + ("?" + "&".join(params) if params else "")
    webbrowser.open(url)
    return page


class MessageDetailScreen(ModalScreen[None]):
    """The whole matched message, every field, with the terms highlighted."""

    CSS = """
    MessageDetailScreen { align: center middle; }
    #detail-box {
        width: 92%;
        height: 90%;
        border: solid $primary;
        background: $surface;
    }
    #detail-header {
        dock: top;
        height: auto;
        padding: 0 1;
        background: $primary;
        color: $text;
    }
    #detail-body { padding: 0 1; }
    .field-title { color: $accent; text-style: bold; margin-top: 1; }
    #detail-footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "dismiss", "Kapat"),
        Binding("o", "open_html", "HTML'de aç"),
    ]

    def __init__(
        self,
        result: fts.SearchResult,
        fields: dict[str, str],
        terms: list[str],
        query: str,
        theme: str,
    ) -> None:
        super().__init__()
        self.result = result
        self.fields = fields
        self.terms = terms
        self.search_query = query
        self.html_theme = theme

    def compose(self) -> ComposeResult:
        r = self.result
        header = (
            f"{project_label(r.project_path)}  ·  {format_timestamp(r.timestamp)}"
            f"  ·  {r.type}  ·  oturum {(r.session_id or '-')[:8]}"
        )
        with Container(id="detail-box"):
            yield Static(header, id="detail-header")
            with VerticalScroll(id="detail-body"):
                shown = False
                for name in fts.SEARCH_FIELDS:
                    value = self.fields.get(name)
                    if not value:
                        continue
                    shown = True
                    yield Static(
                        fts.SEARCH_FIELD_LABELS.get(name, name), classes="field-title"
                    )
                    yield Static(highlighted(value, self.terms))
                if not shown:
                    yield Static("(Bu mesajda gösterilecek metin yok)")
            yield Static(
                "ESC: kapat  ·  o: oturumu tarayıcıda bu mesajda aç", id="detail-footer"
            )

    async def action_dismiss(self, result: None = None) -> None:
        self.dismiss(result)

    def action_open_html(self) -> None:
        _open_html_in_background(self, self.result, self.search_query, self.html_theme)


def _open_html_in_background(
    screen: "Screen[Any]", result: fts.SearchResult, query: str, theme: str
) -> None:
    screen.notify("Oturum sayfası hazırlanıyor…")

    def job() -> None:
        try:
            page = open_session_html(result, query, theme)
            _app_of(screen).call_from_thread(screen.notify, f"Açıldı: {page.name}")
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            _app_of(screen).call_from_thread(
                screen.notify, f"Açılamadı: {e}", severity="error"
            )

    threading.Thread(target=job, daemon=True).start()


class SearchScreen(Screen[None]):
    """Search box + live result table over the whole archive or one project."""

    CSS = """
    SearchScreen { layout: vertical; }
    #search-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    #search-status { height: 1; padding: 0 1; color: $text-muted; }
    #search-results { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Geri"),
        Binding("down", "focus_results", "Sonuçlara in", show=False),
        Binding("ctrl+t", "toggle_scope", "Kapsam: proje/tümü"),
        Binding("o", "open_html", "HTML'de aç"),
        Binding("slash", "focus_input", "Aramaya dön", show=False),
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
        self.html_theme = theme
        self.db_path: Optional[Path] = None
        self.results: list[fts.SearchResult] = []
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
            value=self._query, placeholder=SEARCH_PLACEHOLDER, id="search-input"
        )
        yield Static(self._title(), id="search-title")
        yield Static("Hazırlanıyor…", id="search-status")
        table: DataTable[Text | str] = DataTable(
            id="search-results", cursor_type="row", zebra_stripes=True
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        table.add_column("Tarih", width=16)
        table.add_column("Proje", width=26)
        table.add_column("Tür", width=10)
        table.add_column("Eşleşme")
        search_input = self.query_one(Input)
        search_input.focus()
        search_input.cursor_position = len(self._query)
        self._prepare()

    def _title(self) -> str:
        if self.scoped and self.project_path is not None:
            scope = f"proje: {project_label(str(self.project_path))}"
        else:
            scope = "tüm projeler"
        return f"🔎 Arama — {scope}"

    def _status(self, text: str) -> None:
        self.query_one("#search-status", Static).update(text)

    # -- preparation ------------------------------------------------------

    @work(thread=True, exclusive=True, group="prepare")
    def _prepare(self) -> None:
        worker = _current_worker()

        def progress(msg: str) -> None:
            _app_of(self).call_from_thread(self._status, msg)

        try:
            db_path = prepare_search(
                self.projects_dir, progress, lambda: worker.is_cancelled
            )
        except PrepareError as e:
            _app_of(self).call_from_thread(self._status, f"Arama kullanılamıyor: {e}")
            return
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            _app_of(self).call_from_thread(self._status, f"Hata: {e}")
            return
        _app_of(self).call_from_thread(self._ready, db_path)

    def _ready(self, db_path: Path) -> None:
        self.db_path = db_path
        if self._query:
            self._search(self._query)
        else:
            self._status("Hazır. Yazmaya başla.")

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
            return  # _ready() runs the latest query once the index is built
        if not fts.fts_escape(query):
            self.results = []
            cast("DataTable[Text | str]", self.query_one(DataTable)).clear()
            self._status("Hazır. Yazmaya başla.")
            return
        self._run_query(self.db_path, query, self.project_path if self.scoped else None)

    @work(thread=True, exclusive=True, group="search")
    def _run_query(
        self, db_path: Path, query: str, project_path: Optional[Path]
    ) -> None:
        worker = _current_worker()
        try:
            results = run_search(db_path, query, project_path)
        except sqlite3.Error as e:
            if not worker.is_cancelled:
                _app_of(self).call_from_thread(self._status, f"Sorgu hatası: {e}")
            return
        if not worker.is_cancelled:
            _app_of(self).call_from_thread(self._show, query, results)

    def _show(self, query: str, results: list[fts.SearchResult]) -> None:
        if query != self._query:
            return  # a newer query is on its way
        self.results = results
        terms = _terms(query)
        table = cast("DataTable[Text | str]", self.query_one(DataTable))
        table.clear()
        for i, r in enumerate(results):
            table.add_row(
                format_timestamp(r.timestamp),
                project_label(r.project_path),
                r.type,
                highlighted(r.snippet, terms),
                key=str(i),
            )
        if self._focus_results and results:
            table.focus()
        self._focus_results = False
        if not results:
            self._status("Sonuç yok.")
        else:
            more = "+" if len(results) >= RESULT_LIMIT else ""
            self._status(
                f"{len(results)}{more} sonuç  ·  Enter: mesajı aç  ·  o: tarayıcıda aç"
                "  ·  /: aramaya dön"
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
        if result is None or self.db_path is None:
            return
        try:
            fields = load_message_fields(self.db_path, result.message_id)
        except sqlite3.Error as e:
            self.notify(f"Mesaj okunamadı: {e}", severity="error")
            return
        _app_of(self).push_screen(
            MessageDetailScreen(
                result,
                fields,
                _terms(self._query),
                self._query,
                self.html_theme,
            )
        )

    def action_open_html(self) -> None:
        if isinstance(self.focused, Input):
            return
        result = self._selected()
        if result is None:
            self.notify("Önce bir sonuç seç", severity="warning")
            return
        _open_html_in_background(self, result, self._query, self.html_theme)

    def action_toggle_scope(self) -> None:
        if self.project_path is None:
            self.notify("Zaten tüm projelerde arıyorsun")
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
    SearchBar { margin: 0 0; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "leave", "Listeye dön", show=False),
    ]

    def __init__(
        self,
        projects_dir: Path,
        project_path: Optional[Path] = None,
        theme: str = DEFAULT_THEME,
    ) -> None:
        super().__init__(placeholder=SEARCH_PLACEHOLDER, id="session-lens-search-bar")
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
