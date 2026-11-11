# session-lens

**Browse and search your full Claude Code history, including everything before compaction.**

Claude Code keeps every message in `~/.claude/projects/**/*.jsonl`, but once a
conversation is compacted the UI only shows the summary. session-lens reads those
transcripts directly, so nothing is lost: a terminal UI with full-text search
across every project, plus a local HTML archive.

A lens on your sessions: it reads the transcripts where they are, and browsing
or searching them never changes anything.

session-lens is a fork of [daaain/claude-code-log](https://github.com/daaain/claude-code-log)
(MIT). All of upstream's features are kept; the documentation below the line
is upstream's and applies as-is (use `session-lens` in place of `claude-code-log`).

## What session-lens adds

- **`session-lens` (or the short `slens`) opens the TUI** — no flags
  needed — on **every project's sessions at once**, newest first, wherever
  you start it. `Esc` goes to the project selector: its first row,
  **★ ALL projects**, comes back to all of them; any other row opens that
  one project (where archive/delete/restore live). In a session list,
  `Enter` reads the session in the TUI (see below), `h` opens its HTML page
  and `c` resumes it in Claude Code. Every other subcommand and flag works
  as upstream (`session-lens serve`, `session-lens --open-browser`,
  `session-lens --tui` for upstream's per-directory start, …).
- **Search bar in the TUI** — one line under the project and session
  lists' header. Press `Ctrl+F` (or `/`), or click it, and type: results
  appear as you type, with the matches highlighted. `Ctrl+F` on the results
  takes you back to the box.
  - By default it searches **session names** (the title the list shows),
    newest first.
  - `Ctrl+R` switches to **message content**: full-text search over every
    message, ranked by relevance.
  - `Enter` reads the result's session in the TUI; a message result opens
    at that message with your words already found. `o` opens the session's
    HTML page in the browser instead, scrolled to the message.
  - Each result shows its session's size: message count, tokens and the
    transcript's size on disk.
  - `Ctrl+T` switches between the current project and all projects.
  - The first search brings the cache up to date (and, for message content,
    builds the search index); after that it only reads what changed.
- **Read a session the way Claude Code shows it** — `Enter` on a session
  (or on a search result) opens it full screen as Claude Code's chat screen
  draws it: your prompts behind `❯`, Claude's replies behind `●`, each tool
  call as `● Bash(…)` with its result under `⎿` cut to a few lines, and
  thinking as `✻ Thinking…`, in Claude Code's own (dark theme) colours on
  your terminal's background. Everything compaction hid is there too,
  behind `✻ Compacted`, closed until you click it.
  - Click a long result, a thinking block or a compaction summary to open
    it; `Ctrl+O` opens or closes them all. Claude Code keeps the text of
    only some thinking; one without it says so when opened.
  - As in Claude Code, the prompt that started what is on screen is pinned
    on the top line once it scrolls out of sight (its first paragraph, on
    one line); scrolling past it pins the one before. It shows only while
    you scroll up, so reading on downwards keeps the line, and never at
    the bottom of the conversation. Click it to go to the prompt.
  - Runs of reads and searches fold into one line, as in Claude Code:
    `● Searched for 2 patterns, read 3 files`. Click it to see the calls.
  - Away from the bottom, a `Jump to bottom (End) ↓` button sits at the
    foot of the screen, as in Claude Code; click it or press `End`.
  - Headings, lists, inline code and tables are drawn as Claude Code draws
    them, tables with every border and their cells wrapped to fit.
  - `Ctrl+F` finds text in the conversation, inside folded blocks too, and
    counts the matches (`3/12`): `Enter` or `↓` for the next match, `↑` for
    the previous one. A search result opens at its message
    with the search already run.
  - `Esc` closes the find box, then the conversation. `h` still opens the
    session's HTML page.
  - Long sessions open at once: the end (or the message you opened) is
    drawn first, the rest in the background.
- **Every compaction found** — newer Claude Code versions put a notice
  before some compaction summaries, and those were taken for (very long)
  prompts. They are recognised as compactions again, in the TUI and in
  the HTML pages.
- **Remembered theme** — pick a theme with `Ctrl+P` → *Change theme*; it is
  saved to `~/.config/session-lens/tui-theme` (a theme saved under the
  project's earlier names, session-rewind and cclens, is still read).
- **Minimal HTML theme by default** (override with `--theme classic` or
  `CLAUDE_CODE_LOG_THEME`).

## Install

```bash
pipx install git+https://github.com/hanifiakkus/session-lens
session-lens
```

Everything runs locally: no telemetry, and `serve` listens on `127.0.0.1` only.

To work on it, install your clone in editable mode, so your changes apply
at once:

```bash
git clone https://github.com/hanifiakkus/session-lens
cd session-lens
uv sync                    # the development environment (.venv)
pipx install --editable .  # the session-lens / slens commands
just ci                    # before committing
```

An editable install points at the clone's folder. If you move or rename
the folder, run `pipx install --force --editable .` again in its new place
(and `rm -rf .venv && uv sync`); otherwise the commands fail with
`ModuleNotFoundError: No module named 'claude_code_log'`.

The import package keeps upstream's name, `claude_code_log`, so merges from
upstream stay simple.

Versions are upstream's release plus a fork number, e.g. `1.7.0+lens.1`:
session-lens built on upstream 1.7.0, its first release. The cache's
compatibility rules and the HTML pages' freshness are keyed on upstream's
numbers, so the base stays upstream's; a new version re-renders the HTML
pages, and its fork number restarts at 1 when the base moves on.

---

# Claude Code Log (upstream documentation)

A Python CLI tool that converts Claude Code transcript JSONL files into readable HTML and Markdown (plus JSON for tooling).

Quick demo:

[Supercut](https://github.com/user-attachments/assets/cc7eb87a-4f8e-416c-ad6b-f6abc635ad59)

▶️ **[Feature walkthrough](https://daaain.github.io/claude-code-log/demos/)**: six short chapters on the archive index and search, reading a session, folding and filtering, agents and forks, and live sessions.

TUI demo:

[TUI](https://github.com/user-attachments/assets/75718e2b-3b02-4e17-8f3d-366e2c40dcc2)

## Project Overview

📋 **[View Changelog](CHANGELOG.md)** - See what's new in each release

This tool generates clean, minimalist HTML pages showing user prompts and assistant responses chronologically. It's designed to create a readable log of your Claude Code interactions with support for both individual files and entire project hierarchies.

> [!NOTE]
> Experimental provider support is now available for single-session exports from
> Antigravity CLI (`agy`, **alpha**) and Codex CLI (`codex`, **beta**). Use
> `--provider agy|codex --session-id <id>`; these integrations may change as the
> upstream transcript formats evolve.

📄 **[View Example HTML Output](https://daaain.github.io/claude-code-log/example/)** - A real example generated from a sample of this project's development, regenerated on every docs build

## Quickstart

TL;DR: run the command below and browse the pages generated from your entire Claude Code archives:

```sh
uvx claude-code-log@latest --open-browser
```

## Key Features

- **Interactive TUI (Terminal User Interface)**: Browse and manage Claude Code sessions with real-time navigation, summaries, and quick actions for HTML export and session resuming
- **Watch Mode**: `claude-code-log watch` re-converts as a session is written, so Markdown in an editor or Obsidian stays current; `claude-code-log serve --watch` makes an open session page grow as messages arrive, keeping your scroll position and folded sections
- **Project Hierarchy Processing**: Process entire `~/.claude/projects/` directory with linked index page
- **Individual Session Files**: Generate separate HTML files for each session with navigation links
- **Single File or Directory Processing**: Convert individual JSONL files or specific directories
- **Session Navigation**: Interactive table of contents with session summaries and quick navigation
- **Token Usage Tracking**: Display token consumption for individual messages and session totals
- **Runtime Message Filtering**: JavaScript-powered filtering to show/hide message types (user, assistant, system, tool use, etc.)
- **Chronological Ordering**: All messages sorted by timestamp across sessions
- **Interactive timeline**: Generate an interactive, zoomable timeline grouped by message times to navigate conversations visually
- **Themes**: `--theme minimal` gives a compact light/dark page where sub-agents and rewind forks are branches you can fold away, interleave with the main conversation or open side by side in columns (see [Choosing a Theme](#choosing-a-theme))
- **Cross-Session Summary Matching**: Properly match async-generated summaries to their original sessions
- **Date Range Filtering**: Filter messages by date range using natural language (e.g., "today", "yesterday", "last week")
- **Rich Message Types**: Support for user/assistant messages, tool use/results, thinking content, images
- **System Command Visibility**: Show system commands (like `init`) in expandable details with structured parsing
- **Markdown Rendering**: Server-side markdown rendering with syntax highlighting using wenmode
- **Archive Search**: `claude-code-log serve` serves the archive over loopback with full-text search across every project and session (see the [archive search guide](https://daaain.github.io/claude-code-log/archive-search/)); every generated page also has in-page search
- **Depth Levels & Compact Mode**: `--depth session|user|assistant|agent|tool|hook` sets how deep into the message hierarchy to render and `--compact` merges repeated section headings — pairs well with `--format md` to feed past conversations back to an LLM for analysis or experience building
- **Floating Navigation**: Always-available back-to-top button and filter controls
- **CLI Interface**: Simple command-line tool using Click

## What Problems Does This Solve?

This tool helps you answer questions like:

- **"How can I review all my Claude Code conversations?"**
- **"What did I work on with Claude yesterday/last week?"**
- **"How many tokens are my Claude Code sessions using?"**
- **"How can I search through my entire Claude Code history?"**
- **"What tools did Claude use in this project?"**
- **"How can I share my Claude Code conversation with others?"**
- **"What's the timeline of my project development?"**
- **"How can I analyse patterns in my Claude Code usage?"**
- **"How can I feed a past session back to an LLM for analysis or experience building?"**

## Usage

### Interactive TUI (Terminal User Interface)

The TUI provides an interactive interface for browsing and managing Claude Code sessions with real-time navigation, session summaries, and quick actions.

```bash
# Launch TUI for all projects (default behavior)
claude-code-log --tui

# Launch TUI for specific project directory
claude-code-log /path/to/project --tui

# Launch TUI for specific Claude project
claude-code-log my-project --tui  # Automatically converts to ~/.claude/projects/-path-to-my-project
```

**TUI Features:**

- **Session Listing**: Interactive table showing session IDs, summaries, timestamps, message counts, and token usage
- **Smart Summaries**: Prioritizes Claude-generated summaries over first user messages for better session identification
- **Working Directory Matching**: Automatically finds and opens projects matching your current working directory
- **Quick Actions**:
  - `h`: Generate and open session HTML in browser
  - `m`: Generate and open session Markdown in browser
  - `v`: View session Markdown in embedded viewer (with table of contents)
  - `c`: Resume session in Claude Code with `claude -r <sessionId>`
  - `a` / `d`: Archive or delete the selected session
  - `r`: Restore an archived session back to a JSONL file
  - `e`: Toggle the expanded view of the selected row
  - `p`: Switch to project selector view
  - `?`: Show all keybindings
  - `H`/`M`/`V`: Force regenerate HTML/Markdown (hidden shortcuts for development)
- **Project Statistics**: Real-time display of total sessions, messages, tokens, and date range
- **Cache Integration**: Leverages existing cache system for fast loading with automatic cache validation
- **Keyboard Navigation**: Arrow keys to navigate, Enter to open the session's HTML, `q` to quit
- **Row Expansion**: Press `e` to expand the selected row, showing full summary, first user message, working directory, and detailed token usage

### Default Behavior (Process All Projects)

```bash
# Process all projects in ~/.claude/projects/ (default behavior)
claude-code-log

# Explicitly process all projects
claude-code-log --all-projects

# Process all projects and open in browser
claude-code-log --open-browser

# A view of a date window, in a directory of its own
claude-code-log --from-date "yesterday" --to-date "today" -o ~/yesterday
claude-code-log --from-date "last week" -o ~/last-week

# Skip individual session files (only create combined transcripts)
claude-code-log --no-individual-sessions
```

This creates:

- `~/.claude/projects/index.html` - Top level index with project cards and statistics
- `~/.claude/projects/project-name/combined_transcripts.html` - Individual project pages (these can be several megabytes)
- `~/.claude/projects/project-name/session-{session-id}.html` - Individual session pages
- `~/.claude/projects/project-name/session-{session-id}.md` - Markdown versions (with `--format md`, or on demand from the TUI)

### Single File or Directory Processing

```bash
# Single file
claude-code-log transcript.jsonl

# Specific directory
claude-code-log /path/to/transcript/directory

# Custom output location
claude-code-log /path/to/directory -o combined_transcripts.html

# Open in browser after conversion
claude-code-log /path/to/directory --open-browser

# Filter by date range (supports natural language)
claude-code-log /path/to/directory --from-date "yesterday" --to-date "today" --combined only -o ~/yesterday.html
claude-code-log /path/to/directory --from-date "3 days ago" --to-date "yesterday" --force
```

A date-filtered run writes a view of its window: only the projects and
sessions with messages in it, with the window's totals. It goes into a
destination that holds none of the pages it writes, so over the pages of
an earlier run it refuses to start: give it another `-o`, or `--force` to
write over them (nothing is removed). For one directory, `-o` names the
combined page alone and the session pages go into the directory, hence
`--combined only` above. See [Viewing a date
window](https://daaain.github.io/claude-code-log/date-window/).

### Feeding Past Conversations to an LLM

The combination `--depth agent --format md --compact` produces condensed Markdown suitable as context for an LLM to review or distill patterns from past work:

```bash
# Session → compact Markdown for LLM review
claude-code-log transcript.jsonl --depth agent --format md --compact -o session.md

# Whole project history
claude-code-log /path/to/project --depth agent --format md --compact
```

`--depth` levels (smallest → largest output):

- `session` — session structure only (headers and navigation)
- `user` — just user prompts and steering (useful as input to a downstream agent, e.g. building a requirements doc)
- `assistant` — user + assistant text only
- `agent` — adds sub-agents and key tool signals (WebSearch, WebFetch, agent delegations)
- `tool` — adds tools, cleaned of system/hook noise (default)
- `hook` — everything, including hooks and system notices

`--detail full|high|low|minimal|user-only` still works but is deprecated (removed in 2.0); it maps to `hook`, `tool`, `agent`, `assistant` and `user` respectively. Add `--no-recaps` to drop the `※ recap` summaries, which otherwise appear at every depth.

`--compact` merges consecutive same-type sections in Markdown so runs of assistant responses share one heading instead of repeating `### 🤖 Assistant:` for each.

### Linking Commit SHAs

Plain `7c2e6f6`-shaped tokens in transcript prose get turned into clickable commit links when the SHA is reachable from a local remote-tracking branch. **github.com**, **gitlab.com**, and **bitbucket.org** work out of the box. For self-hosted forges (in-house GitLab, Gitea, Forgejo, …), supply a URL template via `--git-link`:

```bash
# Self-hosted GitLab
claude-code-log /path/to/transcript --git-link 'https://{host}/{path}/-/commit/{sha}'

# Same thing via env var (useful for TUI / repeated invocations)
export CLAUDE_CODE_LOG_GIT_LINK='https://{host}/{path}/-/commit/{sha}'
claude-code-log --tui
```

Placeholders: `{host}`, `{path}`, `{sha}`. The template fires only when the static map doesn't already know the host, so a mix of GitHub repos + self-hosted GitLab gets correct links from both. SHAs not reachable from any local remote-tracking ref render as plain text — local-only work-in-progress commits never produce broken links.

### Choosing a Theme

HTML output comes in themes, selected with `--theme` (on the default command, `serve` and `watch`, and for the TUI's HTML exports) or the `CLAUDE_CODE_LOG_THEME` environment variable:

- `classic` — the original look (the default)
- `minimal` — a compact light/dark theme: dense rows on a time rail, long content folded to short previews, and sub-agents and rewind forks drawn as branches you can fold away, interleave with the main conversation or open side by side in columns
- `default` — the built-in default, currently `classic`

```bash
claude-code-log --theme minimal

# Same thing via env var; an explicit --theme always wins over it
export CLAUDE_CODE_LOG_THEME=minimal
claude-code-log --theme classic   # still classic
```

Themes write the same files (`combined_transcripts.html`, `session-*.html`, …) rather than separate ones, and each page records the theme it was rendered in, so switching theme regenerates the whole archive (and switching back regenerates it again). `--theme` is HTML-only; with `--format md` it is ignored with a warning. An unknown `CLAUDE_CODE_LOG_THEME` value is an error. The [themes guide](https://daaain.github.io/claude-code-log/themes/) walks through the minimal theme's toolbar, branch modes and dark mode, with screenshots.

![The minimal theme with two background agents folded to one line each](docs/assets/themes/main-light.png)

## Project Hierarchy Output

When processing all projects, the tool generates:

```sh
~/.claude/projects/
├── index.html                           # Master index with project cards
├── project1/
│   ├── combined_transcripts.html        # Combined project page
│   ├── session-{session-id}.html        # Individual session pages
│   ├── session-{session-id}.md          # Markdown version (--format md or TUI)
│   └── session-{session-id2}.html       # More session pages...
├── project2/
│   ├── combined_transcripts.html
│   └── session-{session-id}.html
└── ...
```

### Index Page Features

- **Project Cards**: Each project shown as a clickable card with statistics
- **Session Navigation**: Expandable session list with summaries and quick access to individual session files
- **Summary Statistics**: Total projects, transcript files, and message counts with token usage
- **Recent Activity**: Projects sorted by last modification date
- **Quick Navigation**: One-click access to combined transcripts or individual sessions
- **Clean URLs**: Readable project names converted from directory names

## Message Types Supported

- **User Messages**: Regular user inputs and prompts
- **Assistant Messages**: Claude's responses with token usage display
- **Summary Messages**: Session summaries with cross-session matching
- **System Commands**: Commands like `init` shown in expandable details with structured parsing
- **Tool Use**: Tool invocations with collapsible details and special TodoWrite rendering
- **Tool Results**: Tool execution results with error handling
- **Thinking Content**: Claude's internal reasoning processes
- **Images**: Pasted images and screenshots

## HTML Output Features

- **Responsive Design**: Works on desktop and mobile
- **Runtime Message Filtering**: JavaScript controls to show/hide message types with live counts
- **Session Navigation**: Interactive table of contents with session summaries and timestamp ranges
- **Token Usage Display**: Individual message and session-level token consumption tracking
- **Syntax Highlighting**: Code blocks properly formatted with markdown rendering
- **Markdown Support**: Server-side rendering with wenmode including:
  - Headers, lists, emphasis, strikethrough
  - Code blocks and inline code
  - Links, images, and tables
  - GitHub Flavored Markdown features
- **Collapsible Content**: Tool use, system commands, and long content in expandable sections
- **Floating Controls**: Always-available filter button, details toggle, and back-to-top navigation
- **Cross-Session Features**: Summaries properly matched across async sessions

## Markdown Output Features

Markdown export provides a lightweight, portable alternative to HTML:

- **GitHub-Flavored Markdown**: Compatible with GitHub, GitLab, and other Markdown renderers
- **Hierarchical Structure**: Sessions organized with headers and collapsible details
- **Message Excerpts**: Section titles include message previews for quick navigation
- **Code Preservation**: Syntax highlighting hints via fenced code blocks
- **Embedded Viewer**: TUI includes built-in Markdown viewer with table of contents
- **Image Support**: Configurable image handling (placeholder, embedded base64, or referenced files)
- **`--compact` Mode**: Merge consecutive same-type section headings — most useful with `--depth agent` or `assistant` where tool stripping creates runs of Assistant or User sections

## Installation

Requires Python 3.10 or later, linked against **SQLite 3.35 or later** for the
cache (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` prints the
version yours uses). The Pythons `uv` installs bundle a recent SQLite; a
distribution's system Python links the system's own, which can be older. With
an older one, `claude-code-log` stops with an error naming both versions, and
`--no-cache` converts without the cache.

The recommended way is installing as a `uv` tool:

```bash
uv tool install claude-code-log
```

This will create a `claude-code-log` binary that you can call directly.

Or run directly with `uvx` (no separate installation step required):

```bash
uvx claude-code-log@latest
```

Alternatively, if you prefer, install using `pip`:

```bash
pip install claude-code-log
```

Or install from source:

```bash
git clone https://github.com/daaain/claude-code-log.git
cd claude-code-log
uv sync
uv run claude-code-log
# or if you want `claude-code-log` binary that works everywhere:
uv tool install --editable .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and architecture documentation.

## Community Extensions

Projects built on top of `claude-code-log`:

- **[archive-session](https://github.com/lifeinchords/claude-code-skills#archive-session-skill--slash-command--optional-hook)** by [@lifeinchords](https://github.com/lifeinchords). Wraps the CLI as three integration surfaces:
  - a Claude Code [Skill](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/skills/archive-session/SKILL.md)
  - a Claude Code slash [Command](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/commands/archive-session.md) `/archive-session` for explicit in-chat invocation
  - a Claude Code PreCompact [Hook](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/hooks/pre-compact-archive.sh) that auto-archives transcripts and subagent logs right before context compaction

Cross-platform (macOS and Windows/MSYS).

## TODO

- tutorial overlay
- integrate `claude-trace` request logs if present?
- convert images to WebP as screenshots are often huge PNGs – this might be time consuming to keep redoing (so would also need some caching) and need heavy dependencies with compilation (unless there are fast pure Python conversion libraries? Or WASM?)
- add special formatting for built-in tools: LS, NotebookRead, NotebookEdit, TodoRead
- add `ccusage` like daily summary and maybe some textual summary too based on Claude generate session summaries?
- import logs from @claude Github Actions
- stream logs from @claude Github Actions, see [octotail](https://github.com/getbettr/octotail)
- wrap up CLI as Github Action to run after Claude Github Action and process [output](https://github.com/anthropics/claude-code-base-action?tab=readme-ov-file#outputs)
- feed the filtered user messages to headless claude CLI to distill the user intent from the session
- animate gradient background in the classic theme
- merge git worktree directories
