from __future__ import annotations

import click

from anki_cli.backends.protocol import JSONValue
from anki_cli.cli.command import CommandContext, anki_command


def _deck_chain(name: str) -> list[str]:
    raw = name.strip()
    if not raw:
        raise ValueError("Deck name cannot be empty.")

    parts = [part.strip() for part in raw.split("::")]
    if any(not part for part in parts):
        raise ValueError("Deck hierarchy has empty segment(s). Use names like A::B::C.")

    chain: list[str] = []
    for idx in range(1, len(parts) + 1):
        chain.append("::".join(parts[:idx]))
    return chain


def _parse_step_values(raw: str | None) -> list[float] | None:
    if raw is None:
        return None
    cleaned = [part.strip() for part in raw.split(",") if part.strip()]
    if not cleaned:
        return []
    return [float(value) for value in cleaned]


def _require_deck_name(cmd: CommandContext, deck_name: str) -> str:
    normalized = deck_name.strip()
    if not normalized:
        raise cmd.invalid("Deck name cannot be empty.")
    return normalized


@anki_command("decks")
def decks_cmd(cmd: CommandContext) -> JSONValue:
    """List all decks with due counts."""
    backend = cmd.backend
    # One canonical shape for every --format (#28). The table renderer
    # indents ``name`` by ``level`` itself; data never changes with format.
    items: list[dict[str, JSONValue]] = []
    for deck in backend.get_decks():
        deck_name = str(deck.get("name", "")).replace("\x1f", "::")
        due = backend.get_due_counts(deck=deck_name)
        parts = [part for part in deck_name.split("::") if part]
        items.append(
            {
                **deck,
                "name": deck_name,
                "new": due.get("new", 0),
                "learn": due.get("learn", 0),
                "review": due.get("review", 0),
                "total_due": due.get("total", 0),
                "level": max(0, len(parts) - 1),
            }
        )
    return {"count": len(items), "items": items}


@anki_command("deck", errors={LookupError: ("ENTITY_NOT_FOUND", 4)})
@click.option("--deck", "deck_name", required=True, help="Deck name")
def deck_cmd(cmd: CommandContext, deck_name: str) -> JSONValue:
    """Show details for a single deck."""
    normalized = _require_deck_name(cmd, deck_name)
    with cmd.errors(details={"deck": normalized}):
        return cmd.backend.get_deck(normalized)


@anki_command("deck:create")
@click.option("--deck", "--name", "name", required=True, help="Deck name, e.g. Japanese::Vocab")
def deck_create_cmd(cmd: CommandContext, name: str) -> JSONValue:
    """Create a new deck (supports A::B hierarchy)."""
    _require_deck_name(cmd, name)
    try:
        chain = _deck_chain(name)
    except ValueError as exc:
        raise cmd.invalid(str(exc)) from exc

    created: list[dict[str, JSONValue]] = []
    existing: list[dict[str, JSONValue]] = []
    for item in chain:
        result = cmd.backend.create_deck(name=item)
        if bool(result.get("created", True)):
            created.append(result)
        else:
            existing.append(result)
    return {
        "requested": name.strip(),
        "chain": chain,
        "created_count": len(created),
        "existing_count": len(existing),
        "created": created,
        "existing": existing,
    }


@anki_command(
    "deck:rename",
    errors={LookupError: ("ENTITY_NOT_FOUND", 4), ValueError: ("INVALID_INPUT", 2)},
)
@click.option("--from", "from_name", required=True, help="Current deck name")
@click.option("--to", "to_name", required=True, help="New deck name")
def deck_rename_cmd(cmd: CommandContext, from_name: str, to_name: str) -> JSONValue:
    """Rename an existing deck."""
    source = from_name.strip()
    target = to_name.strip()
    if not source or not target:
        raise cmd.invalid("Both --from and --to are required.")
    try:
        _deck_chain(target)
    except ValueError as exc:
        raise cmd.invalid(str(exc)) from exc
    with cmd.errors(details={"from": source, "to": target}):
        return cmd.backend.rename_deck(old_name=source, new_name=target)


@anki_command("deck:delete", errors={ValueError: ("INVALID_INPUT", 2)})
@click.option("--deck", "deck_name", required=True, help="Deck name to delete")
def deck_delete_cmd(cmd: CommandContext, deck_name: str) -> JSONValue:
    """Delete a deck (requires --yes)."""
    cmd.require_yes("Deleting a deck requires --yes.")
    stripped = deck_name.strip()
    with cmd.errors(details={"deck": stripped}):
        return cmd.backend.delete_deck(name=stripped)


@anki_command(
    "deck:config",
    errors={LookupError: ("ENTITY_NOT_FOUND", 4), ValueError: ("ENTITY_NOT_FOUND", 4)},
)
@click.option("--deck", "deck_name", required=True, help="Deck name")
def deck_config_cmd(cmd: CommandContext, deck_name: str) -> JSONValue:
    """Show scheduler config for a deck."""
    normalized = _require_deck_name(cmd, deck_name)
    with cmd.errors(details={"deck": normalized}):
        return cmd.backend.get_deck_config(normalized)


@anki_command(
    "deck:config:set",
    errors={
        LookupError: ("BACKEND_OPERATION_FAILED", 1),
        ValueError: ("BACKEND_OPERATION_FAILED", 1),
    },
)
@click.option("--deck", "deck_name", required=True, help="Deck name")
@click.option("--new-per-day", type=int, default=None)
@click.option("--reviews-per-day", type=int, default=None)
@click.option("--desired-retention", type=float, default=None)
@click.option("--maximum-review-interval", type=int, default=None)
@click.option("--learn-steps", default=None, help="Comma-separated values, in minutes.")
@click.option("--relearn-steps", default=None, help="Comma-separated values, in minutes.")
def deck_config_set_cmd(
    cmd: CommandContext,
    deck_name: str,
    new_per_day: int | None,
    reviews_per_day: int | None,
    desired_retention: float | None,
    maximum_review_interval: int | None,
    learn_steps: str | None,
    relearn_steps: str | None,
) -> JSONValue:
    """Update scheduler settings for a deck."""
    normalized = _require_deck_name(cmd, deck_name)
    try:
        parsed_learn_steps = _parse_step_values(learn_steps)
        parsed_relearn_steps = _parse_step_values(relearn_steps)
    except ValueError as exc:
        raise cmd.invalid(f"Failed to parse step values: {exc}") from exc

    updates: dict[str, JSONValue] = {}
    if new_per_day is not None:
        updates["new_per_day"] = new_per_day
    if reviews_per_day is not None:
        updates["reviews_per_day"] = reviews_per_day
    if desired_retention is not None:
        updates["desired_retention"] = desired_retention
    if maximum_review_interval is not None:
        updates["maximum_review_interval"] = maximum_review_interval
    if parsed_learn_steps is not None:
        updates["learn_steps"] = parsed_learn_steps
    if parsed_relearn_steps is not None:
        updates["relearn_steps"] = parsed_relearn_steps
    if not updates:
        raise cmd.invalid("Provide at least one update option.")

    with cmd.errors(details={"deck": normalized, "updates": updates}):
        return cmd.backend.set_deck_config(normalized, updates)
