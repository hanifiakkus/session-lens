"""session-lens entry point and small local additions shared by the TUI.

``session-lens`` is upstream's ``claude-code-log`` CLI with different defaults:

* no arguments opens the TUI (upstream converts every project to HTML),
  on every project's sessions at once; Esc goes to the project selector,
  whose first row comes back to all of them;
* HTML exports use the ``minimal`` theme unless ``--theme`` or
  ``CLAUDE_CODE_LOG_THEME`` says otherwise.

Everything else is passed through untouched, so ``session-lens serve``,
``session-lens --open-browser`` etc. behave exactly like upstream.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from textual.app import App

#: Where the TUI remembers the Textual theme picked with Ctrl+P.
THEME_FILE = Path.home() / ".config" / "session-lens" / "tui-theme"
#: Where it lived under the project's earlier names (session-rewind, cclens);
#: read as fallbacks, newest name first, until a theme is saved again.
LEGACY_THEME_FILES = (
    Path.home() / ".config" / "session-rewind" / "tui-theme",
    Path.home() / ".config" / "cclens" / "tui-theme",
)
FALLBACK_THEME = "gruvbox"


def load_theme(app: "App[Any]") -> str:
    """The saved TUI theme, or the fallback if none/unknown."""
    name = ""
    for path in (THEME_FILE, *LEGACY_THEME_FILES):
        try:
            name = path.read_text(encoding="utf-8").strip()
            break
        except OSError:
            continue
    return name if name in app.available_themes else FALLBACK_THEME


def save_theme(name: str) -> None:
    try:
        THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
        THEME_FILE.write_text(name, encoding="utf-8")
    except OSError:
        pass  # not being able to remember a theme is not worth a crash


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


#: What the project selector returns for its "all projects" row. Not a real
#: directory: it only ever travels between the selector and ``run_tui``.
ALL_PROJECTS = Path("<all projects>")
ALL_PROJECTS_LABEL = "★ ALL projects"


def _prepare_all(projects_dir: Path) -> Optional[Path]:
    """Refresh every project's cache, printing progress; the cache path."""
    import shutil

    from .tui_search import PrepareError, prepare_search

    width = shutil.get_terminal_size((80, 20)).columns - 1

    def progress(message: str) -> None:
        print("\r" + message[:width].ljust(width), end="", flush=True)

    try:
        db_path = prepare_search(projects_dir, progress, lambda: False)
    except PrepareError as e:
        print(f"\nCannot list sessions: {e}")
        return None
    print("\r" + " " * width + "\r", end="", flush=True)
    return db_path


def run_tui(projects_dir: Path, theme: str) -> None:
    """The TUI, starting on every project's sessions.

    Esc in any session list goes to the project selector; its first row
    ("ALL projects") returns to the list of all sessions, any other row
    opens that project in upstream's session browser.
    """
    from .cli import (  # pyright: ignore[reportPrivateUsage]
        _discover_projects,  # pyright: ignore[reportPrivateUsage]
        _launch_tui_with_cache_check,  # pyright: ignore[reportPrivateUsage]
    )
    from .cli import find_projects_by_cwd
    from .tui import run_project_selector
    from .tui_all import BACK_TO_PROJECTS, run_all_sessions_browser

    if not projects_dir.is_dir():
        print(f"Error: Projects directory not found: {projects_dir}")
        return
    project_dirs, archived = _discover_projects(projects_dir)
    if not project_dirs and not archived:
        print(f"No projects with JSONL files found in {projects_dir}")
        return
    matching = find_projects_by_cwd(projects_dir)

    current = ALL_PROJECTS
    while True:
        if current == ALL_PROJECTS:
            db_path = _prepare_all(projects_dir)
            if db_path is None:
                return
            result = run_all_sessions_browser(projects_dir, db_path, theme)
        else:
            result = _launch_tui_with_cache_check(
                current, is_archived=current in archived, theme=theme
            )
        if result != BACK_TO_PROJECTS:
            return
        project_dirs, archived = _discover_projects(projects_dir)
        selected = run_project_selector(
            project_dirs, matching, archived, include_all=True
        )
        if not selected:
            return
        current = selected


def _with_defaults(argv: list[str]) -> list[str]:
    args = list(argv)
    if not args:
        args = ["--tui"]
    return args


def main(argv: Optional[list[str]] = None) -> None:
    os.environ.setdefault("CLAUDE_CODE_LOG_THEME", "minimal")
    raw = sys.argv[1:] if argv is None else argv
    if not raw:
        from .cli import _resolve_theme  # pyright: ignore[reportPrivateUsage]

        run_tui(default_projects_dir(), _resolve_theme(None))
        return

    from .cli import main as upstream_main

    args = _with_defaults(raw)
    prog = Path(sys.argv[0]).name if argv is None else "session-lens"
    if prog not in ("session-lens", "slens"):
        prog = "session-lens"
    upstream_main(args=args, prog_name=prog)


if __name__ == "__main__":
    main()
