"""session-lens entry point and small local additions shared by the TUI.

``session-lens`` is upstream's ``claude-code-log`` CLI with different defaults:

* no arguments opens the TUI (upstream converts every project to HTML);
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


def _with_defaults(argv: list[str]) -> list[str]:
    args = list(argv)
    if not args:
        args = ["--tui"]
    return args


def main(argv: Optional[list[str]] = None) -> None:
    os.environ.setdefault("CLAUDE_CODE_LOG_THEME", "minimal")
    from .cli import main as upstream_main

    args = _with_defaults(sys.argv[1:] if argv is None else argv)
    prog = Path(sys.argv[0]).name if argv is None else "session-lens"
    if prog not in ("session-lens", "slens"):
        prog = "session-lens"
    upstream_main(args=args, prog_name=prog)


if __name__ == "__main__":
    main()
