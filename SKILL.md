---
name: anki-cli
description: Manage Anki flashcards via CLI with structured JSON output. Create, query, review, and organize cards, notes, decks, tags, and notetypes. Supports AnkiConnect and direct SQLite backends. Use when the user mentions Anki, flashcards, spaced repetition, SRS, reviewing cards, or managing decks.
---

# anki-cli

A hybrid Anki CLI that works with both AnkiConnect (running Anki Desktop) and direct SQLite access. All commands emit structured output suitable for programmatic consumption.

## Quick Setup

```bash
anki status          # check backend and collection health
anki --format json status  # structured output for parsing
```

### Backend Selection

| Flag | Behavior |
|------|----------|
| `--backend auto` | Auto-detect best available (default) |
| `--backend ankiconnect` | Requires running Anki Desktop with AnkiConnect |
| `--backend direct` | Operates on SQLite collection file directly |
| `--backend direct --col /path/to/collection.anki2` | Explicit collection path |

Environment variable `ANKI_CLI_BACKEND` sets the default.

## Output Formats

Always use `--format json` when parsing output programmatically.

```bash
anki --format json cards --query "deck:Default"
```

Available: `json`, `table`, `md`, `csv`, `plain`.

### JSON Response Structure

Success:

```json
{
  "ok": true,
  "data": { "...": "..." },
  "meta": {
    "command": "cards",
    "backend": "direct",
    "collection": "/path/to/collection.anki2",
    "timestamp": "2026-02-21T12:00:00Z"
  }
}
```

Error:

```json
{
  "ok": false,
  "error": {
    "code": "ENTITY_NOT_FOUND",
    "message": "Deck not found: NonExistent",
    "details": {}
  },
  "meta": { "...": "..." }
}
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Backend operation failed |
| 2 | Invalid input, confirmation required, or unsupported operation |
| 4 | Entity not found |
| 7 | Backend unavailable |

### Error Codes

`BACKEND_UNAVAILABLE`, `INVALID_INPUT`, `ENTITY_NOT_FOUND`, `BACKEND_OPERATION_FAILED`, `CONFIRMATION_REQUIRED`, `UNDO_EMPTY`, `TUI_NOT_AVAILABLE`, `UNSUPPORTED_BACKEND`.

## Command Reference

### Querying

```bash
anki cards --query "deck:Japanese is:due"
anki notes --query "tag:verb"
anki search --query "(tag:verb OR tag:noun) -is:suspended"
```

### Inspecting Entities

```bash
anki card --id <card_id>
anki note --id <note_id>
anki deck --deck "Default"
anki notetype --name "Basic"
anki tag --tag "verb"
```

### Listing

```bash
anki decks          # all decks with due counts
anki notetypes      # all note types
anki tags           # all tags with counts
```

### Creating Notes

```bash
anki note:add --deck "Default" --notetype "Basic" --Front "Question" --Back "Answer"
anki note:add --deck "Default" --notetype "Basic" --Front "Q" --Back "A" --tags "tag1,tag2"
```

Field names are passed as dynamic CLI options matching the notetype's field names.

### Bulk Adding Notes

Accepts JSON array from stdin or a file:

```bash
anki note:bulk --deck "Default" --notetype "Basic" --file notes.json
```

JSON format: `[{"Front": "Q1", "Back": "A1"}, {"Front": "Q2", "Back": "A2"}]`

### Editing Notes

```bash
anki note:edit --id <note_id> --Front "Updated question"
anki note:edit --id <note_id> --tags "new-tag1,new-tag2"
```

### Deleting (Destructive -- requires `--yes`)

```bash
anki note:delete --id <note_id> --yes
anki deck:delete --deck "Temporary" --yes
```

### Card Actions

```bash
anki card:suspend --query "deck:Old"
anki card:unsuspend --id <card_id>
anki card:move --query "tag:relocate" --deck "Archive"
anki card:flag --id <card_id> --flag 3
anki card:bury --query "deck:Default"
anki card:unbury --deck "Default"
anki card:reschedule --query "tag:reset" --days 5
anki card:reset --query "tag:relearn"
anki card:revlog --id <card_id> --limit 20
```

Most card actions accept either `--id <card_id>` for a single card or `--query "<search>"` for bulk operations.

### Tags

```bash
anki tag:add --query "deck:Default" --tag "important"
anki tag:remove --id <note_id> --tag "old-tag"
anki tag:rename --from "old" --to "new"
```

### Decks

```bash
anki deck:create --name "Japanese::Vocab"
anki deck:rename --from "Old Name" --to "New Name"
anki deck:config --deck "Default"
anki deck:config:set --deck "Default" --new-per-day 20 --reviews-per-day 200
```

### Notetypes

```bash
anki notetype:create --name "MyType" --field "Front" --field "Back"
anki notetype:field:add --notetype "Basic" --field "Extra"
anki notetype:field:remove --notetype "Basic" --field "Extra"
anki notetype:template:add --notetype "MyType" --template "Card 1" --front "{{Front}}" --back "{{Back}}"
anki notetype:template:edit --notetype "MyType" --template "Card 1" --front "{{Front}}" --back "{{FrontSide}}<hr>{{Back}}"
anki notetype:css --notetype "Basic"
anki notetype:css --notetype "Basic" --set ".card { font-size: 18px; }"
```

### Review

```bash
anki review                          # due counts
anki review:next --deck "Japanese"   # next due card (question only)
anki review:show --deck "Japanese"   # next card with answer
anki review:answer --id <card_id> --rating good   # answer: again|hard|good|easy
anki review:preview --id <card_id>   # scheduling preview per rating
anki review:undo                     # undo last answer (direct backend only)
```

### Configuration

```bash
anki config                          # show merged config
anki config:path                     # show all paths
anki config:set --key "display.default_output" --value "json"
```

## Search Query Language

### Filters

| Filter | Example |
|--------|---------|
| Deck | `deck:Japanese`, `deck:Japanese*` (glob) |
| Notetype | `notetype:Basic` |
| Tag | `tag:verb`, `tag:lang*` (glob) |
| State | `is:new`, `is:learn`, `is:review`, `is:due`, `is:suspended`, `is:buried` |
| Flag | `flag:1` through `flag:7`, `flag:0` (no flag) |
| Property | `prop:ivl>30`, `prop:due<5`, `prop:reps>=10`, `prop:lapses=0` |
| Note ID | `nid:1234567890` |
| Card ID | `cid:1234567890` |
| Text | bare words or `"quoted phrase"` |

### Logical Operators

- Implicit AND: `deck:Default is:due` (both must match)
- Explicit OR: `tag:verb OR tag:noun`
- NOT / negate: `-is:suspended` or `NOT is:suspended`
- Grouping: `(tag:a OR tag:b) is:new`

## Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `ANKI_CLI_BACKEND` | Default backend | `direct` |
| `ANKI_CLI_OUTPUT` | Default output format | `json` |
| `ANKI_CLI_COLOR` | Enable/disable color | `0` |
| `ANKI_CLI_COLLECTION` | Collection path override | `/path/to/collection.anki2` |

## Config File

Location: `~/.config/anki-cli/config.toml`

```toml
[backend]
prefer = "auto"
ankiconnect_url = "http://localhost:8765"
allow_non_localhost = false

[display]
default_output = "table"
color = true
day_boundary_hour = 4
```

For remote AnkiConnect (e.g. via Tailscale or LAN):

```toml
[backend]
prefer = "ankiconnect"
ankiconnect_url = "http://192.168.1.100:8765"
allow_non_localhost = true
```

## Common Agent Workflows

### Create a deck and populate it

```bash
anki deck:create --name "Spanish::Vocab"
anki note:add --deck "Spanish::Vocab" --notetype "Basic" --Front "hola" --Back "hello"
anki note:add --deck "Spanish::Vocab" --notetype "Basic" --Front "gracias" --Back "thank you"
```

### Query and act on results

```bash
anki --format json cards --query "deck:Default is:due prop:lapses>3"
anki card:suspend --query "deck:Default prop:lapses>5"
anki tag:add --query "deck:Default prop:lapses>3" --tag "leech"
```

### Review loop (programmatic)

```bash
anki --format json review:next --deck "Japanese"
# parse card_id from response
anki --format json review:answer --id <card_id> --rating good
```

### Bulk import from JSON

```bash
echo '[{"Front":"Q1","Back":"A1"},{"Front":"Q2","Back":"A2"}]' | anki note:bulk --deck "Default" --notetype "Basic"
```

### Check health before operations

```bash
anki --format json status
# verify "ok": true before proceeding
```

## Safety

- Destructive commands (`note:delete`, `deck:delete`) require `--yes` or will exit with code 2 and `CONFIRMATION_REQUIRED`.
- Avoid direct-backend writes while Anki Desktop has the collection open.
- Use `--backend ankiconnect` when Anki Desktop is running.
- `review:undo` only works with the direct backend and only undoes the last answer.
