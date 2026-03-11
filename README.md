# anki-cli

Hybrid Anki CLI for humans and agents.

<p align="center">
  <img src="assets/anki-cli.png" alt="anki-cli interactive REPL showing due card counts, keyboard shortcuts, and command autocomplete" width="700">
</p>

`anki-cli` supports both:

- AnkiConnect backend (use a running Anki Desktop instance)
- Direct SQLite backend (operate directly on collection files)

Current release: `0.1.4` (alpha).

## Install

From PyPI (recommended):

```bash
uv tool install anki-cli
anki --version
```

With optional TUI extras:

```bash
uv tool install "anki-cli[tui]"
```

## Quick Start

Inspect environment and backend:

```bash
anki status
anki version
```

List core entities:

```bash
anki decks
anki notetypes
anki tags
```

Query cards and notes:

```bash
anki cards --query "deck:Default is:due"
anki notes --query "tag:verb"
anki search --query "(tag:verb OR tag:noun) -is:suspended"
```

Inspect individual objects:

```bash
anki card --id 1234567890
anki note --id 1234567890
anki deck --deck "Default"
anki notetype --name "Basic"
```

## Backend Modes

Global backend selection:

```bash
anki --backend auto ...
anki --backend ankiconnect ...
anki --backend direct ...
```

Collection override (direct backend):

```bash
anki --backend direct --col "/path/to/collection.anki2" status
```

Backend behavior:

- `auto`: detects and chooses best available backend
- `ankiconnect`: forwards search queries to `findCards` and `findNotes`
- `direct`: compiles queries to SQL and executes directly on the collection DB

### Remote AnkiConnect

To connect to AnkiConnect running on a different machine (e.g. via Tailscale or LAN):

```toml
# ~/.config/anki-cli/config.toml
[backend]
prefer = "ankiconnect"
ankiconnect_url = "http://192.168.1.100:8765"
allow_non_localhost = true
```

The remote Anki Desktop must have AnkiConnect configured to accept non-localhost connections. Set `"webBindAddress"` to a specific interface address (e.g. your Tailscale IP) or `"0.0.0.0"` for all interfaces. If binding to all interfaces, consider restricting access with firewall rules.

## Search Query Language

Supported filters:

- `deck:NAME` (supports `*` glob)
- `notetype:NAME`
- `tag:NAME` (supports `*` glob)
- `is:new`, `is:learn`, `is:review`, `is:due`, `is:suspended`, `is:buried`
- `flag:N`
- `prop:ivl>N`, `prop:due>N`, `prop:reps>N`, `prop:lapses>N` (`<`, `<=`, `=`, `>=`, `>`)
- `nid:ID`, `cid:ID`
- bare text and quoted text (`"specific text"`)

Logical syntax:

- implicit `AND` from whitespace
- explicit `OR`
- unary `-` and `NOT`
- parentheses for grouping

Examples:

```bash
anki cards --query "deck:Japanese is:due"
anki cards --query "tag:verb -is:suspended"
anki notes --query "\"specific text\""
anki cards --query "(tag:a OR tag:b) is:new"
```

## Common Commands

### Cards

```bash
anki cards --query "deck:Default"
anki card --id 123
anki card:suspend --query "is:due"
anki card:unsuspend --id 123
anki card:move --query "tag:to-move" --deck "Archive"
anki card:flag --query "is:review" --flag 3
anki card:bury --query "deck:Default"
anki card:unbury --deck "Default"
anki card:reschedule --query "tag:reset-me" --days 3
anki card:reset --query "tag:relearn"
anki card:revlog --id 123 --limit 20
```

### Notes and tags

```bash
anki notes --query "deck:Default"
anki note --id 123
anki note:add --deck "Default" --notetype "Basic" --Front "Q" --Back "A"
anki note:edit --id 123 --Front "Updated Q" --Back "Updated A"
anki note:fields --id 123
anki note:delete --id 123 --yes
anki tag --tag "verb"
anki tag:add --query "deck:Default" --tag "important"
anki tag:remove --id 123 --tag "important"
anki tag:rename --from "old" --to "new"
```

### Decks and notetypes

```bash
anki deck --deck "Default"
anki deck:create --name "Japanese::Vocab"
anki deck:rename --from "Old" --to "New"
anki deck:delete --deck "Temporary" --yes
anki deck:config --deck "Default"
anki deck:config:set --deck "Default" --new-per-day 20 --reviews-per-day 200

anki notetypes
anki notetype --name "Basic"
anki notetype:create --name "MyType" --field "Front" --field "Back"
anki notetype:field:add --notetype "Basic" --field "Extra"
anki notetype:field:remove --notetype "Basic" --field "Extra"
anki notetype:css --notetype "Basic" --set ".card { font-size: 18px; }"
```

### Review

```bash
anki review
anki review:next
anki review:show
anki review:preview --id 123
anki review:answer --id 123 --rating good
anki review:undo
```

Interactive TUI review (requires TUI extras, direct backend):

```bash
anki review:start --deck "Japanese"
```

## Output and Exit Codes

Global output formats:

```bash
anki --format json ...
anki --format table ...
anki --format md ...
anki --format csv ...
anki --format plain ...
```

Exit codes:

- `0`: success
- `1`: backend operation failed
- `2`: invalid input or confirmation required
- `4`: entity not found
- `7`: backend unavailable

## AI Agent Integration

This repo ships a `SKILL.md` at the project root that AI agents (Cursor, Windsurf, Cline, etc.) can consume to operate `anki-cli` without human guidance. It covers the full command reference, JSON output structure, search query syntax, error codes, and common workflows.

To use it in Cursor, copy or symlink it into your skills directory:

```bash
# Personal skill (available across all projects)
mkdir ~/.cursor/skills/anki-cli
cp SKILL.md ~/.cursor/skills/anki-cli/SKILL.md

# Or project-local skill (shared via the repo)
mkdir .cursor/skills/anki-cli
cp SKILL.md .cursor/skills/anki-cli/SKILL.md
```

For other AI coding agents, point them at `SKILL.md` in the repo root or include it in your agent's context/system prompt.

## Safety Notes

- Use `--yes` for destructive operations (`note:delete`, `deck:delete`).
- In direct mode, avoid write operations while Anki Desktop is open.
- If Anki Desktop is running, prefer `--backend ankiconnect`.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run ty check
uv run pytest
```

With optional TUI dependencies:

```bash
uv sync --group dev --extra tui
uv run pytest -m tui
```

## License

MIT. See `LICENSE`.