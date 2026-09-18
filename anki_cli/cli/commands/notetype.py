from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import backend_session_from_context  # noqa: F401  (patched by tests)
from anki_cli.cli.command import CommandContext, ErrorMap, anki_command
from anki_cli.cli.formatter import formatter_from_ctx  # noqa: F401  (patched by tests)
from anki_cli.models.output import JSONValue


def _default_templates(kind: str) -> tuple[str, str, str]:
    if kind == "cloze":
        return (
            "Cloze",
            "{{cloze:Text}}",
            "{{cloze:Text}}\n\n{{Extra}}",
        )
    return (
        "Card 1",
        "{{Front}}",
        "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
    )


FULL_SYNC_WARNING = (
    "This changed the notetype schema; Anki will require a one-way full sync "
    "(upload) the next time you sync."
)


def _schema_warnings(data: Mapping[str, Any]) -> list[str]:
    return [FULL_SYNC_WARNING] if data.get("full_sync_required") else []


# Every mutating notetype command reports these as an operation failure.
_MUTATION_ERRORS: ErrorMap = {
    AnkiConnectAPIError: ("BACKEND_OPERATION_FAILED", 1),
    LookupError: ("BACKEND_OPERATION_FAILED", 1),
    ValueError: ("BACKEND_OPERATION_FAILED", 1),
}


def _require_name(cmd: CommandContext, notetype_name: str) -> str:
    normalized = notetype_name.strip()
    if not normalized:
        raise cmd.invalid("Notetype name cannot be empty.")
    return normalized


def _require_pair(
    cmd: CommandContext, notetype_name: str, other: str, label: str
) -> tuple[str, str]:
    name, value = notetype_name.strip(), other.strip()
    if not name or not value:
        raise cmd.invalid(f"Both --notetype and --{label} are required.")
    return name, value


@anki_command("notetypes")
def notetypes_cmd(cmd: CommandContext) -> JSONValue:
    """List all note types."""
    items = cmd.backend.get_notetypes()
    return {"count": len(items), "items": items}


@anki_command(
    "notetype",
    errors={AnkiConnectAPIError: ("ENTITY_NOT_FOUND", 4), LookupError: ("ENTITY_NOT_FOUND", 4)},
)
@click.option("--notetype", "--name", "notetype_name", required=True, help="Notetype name")
def notetype_cmd(cmd: CommandContext, notetype_name: str) -> JSONValue:
    """Show details for a note type."""
    normalized = notetype_name.strip()
    if not normalized:
        raise cmd.invalid("Notetype name cannot be empty.", details={"notetype": notetype_name})
    with cmd.errors(details={"notetype": normalized}):
        return cmd.backend.get_notetype(normalized)


@anki_command("notetype:create", errors=_MUTATION_ERRORS)
@click.option("--notetype", "--name", "notetype_name", required=True, help="New notetype name")
@click.option(
    "--kind",
    type=click.Choice(["normal", "cloze"], case_sensitive=False),
    default="normal",
    show_default=True,
)
@click.option("--field", "fields", multiple=True, help="Field name (repeatable)")
@click.option("--template", "template_name", default=None, help="Template name")
@click.option("--front", "front_tmpl", default=None, help="Front template")
@click.option("--back", "back_tmpl", default=None, help="Back template")
@click.option("--css", default="", help="Optional notetype CSS")
def notetype_create_cmd(
    cmd: CommandContext,
    notetype_name: str,
    kind: str,
    fields: tuple[str, ...],
    template_name: str | None,
    front_tmpl: str | None,
    back_tmpl: str | None,
    css: str,
) -> JSONValue:
    """Create a new note type with fields and templates."""
    normalized_name = _require_name(cmd, notetype_name)
    normalized_kind = kind.strip().lower()

    cleaned_fields = [field.strip() for field in fields if field.strip()]
    if not cleaned_fields:
        cleaned_fields = ["Text", "Extra"] if normalized_kind == "cloze" else ["Front", "Back"]

    default_name, default_front, default_back = _default_templates(normalized_kind)
    template = template_name.strip() if template_name else default_name
    front = front_tmpl if front_tmpl is not None else default_front
    back = back_tmpl if back_tmpl is not None else default_back

    with cmd.errors(details={"notetype": normalized_name}):
        return cmd.backend.create_notetype(
            name=normalized_name,
            fields=cleaned_fields,
            templates=[{"name": template, "front": front, "back": back}],
            css=css,
            kind=normalized_kind,
        )


@anki_command("notetype:field:add", errors=_MUTATION_ERRORS)
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--field", "field_name", required=True, help="Field name")
def notetype_field_add_cmd(cmd: CommandContext, notetype_name: str, field_name: str) -> JSONValue:
    """Add a field to a note type."""
    name, field = _require_pair(cmd, notetype_name, field_name, "field")
    with cmd.errors(details={"notetype": name, "field": field}):
        data = cmd.backend.add_notetype_field(name, field)
    cmd.warnings.extend(_schema_warnings(data))
    return data


@anki_command("notetype:field:remove", errors=_MUTATION_ERRORS)
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--field", "field_name", required=True, help="Field name")
def notetype_field_remove_cmd(
    cmd: CommandContext, notetype_name: str, field_name: str
) -> JSONValue:
    """Remove a field from a note type and its value from every note (requires --yes)."""
    name, field = _require_pair(cmd, notetype_name, field_name, "field")
    cmd.require_yes(
        "Removing a field deletes its value from every note of the notetype; requires --yes.",
        details={"notetype": name, "field": field},
    )
    with cmd.errors(details={"notetype": name, "field": field}):
        data = cmd.backend.remove_notetype_field(name, field)
    cmd.warnings.extend(_schema_warnings(data))
    return data


@anki_command("notetype:template:add", errors=_MUTATION_ERRORS)
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--template", "template_name", required=True, help="Template name")
@click.option("--front", "front_tmpl", required=True, help="Front template")
@click.option("--back", "back_tmpl", required=True, help="Back template")
def notetype_template_add_cmd(
    cmd: CommandContext, notetype_name: str, template_name: str, front_tmpl: str, back_tmpl: str
) -> JSONValue:
    """Add a card template to a note type."""
    name, template = _require_pair(cmd, notetype_name, template_name, "template")
    with cmd.errors(details={"notetype": name, "template": template}):
        data = cmd.backend.add_notetype_template(name, template, front_tmpl, back_tmpl)
    cmd.warnings.extend(_schema_warnings(data))
    return data


@anki_command("notetype:template:edit", errors=_MUTATION_ERRORS)
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--template", "template_name", required=True, help="Template name")
@click.option("--front", "front_tmpl", default=None, help="New front template")
@click.option("--back", "back_tmpl", default=None, help="New back template")
def notetype_template_edit_cmd(
    cmd: CommandContext,
    notetype_name: str,
    template_name: str,
    front_tmpl: str | None,
    back_tmpl: str | None,
) -> JSONValue:
    """Edit front/back of a card template."""
    name, template = _require_pair(cmd, notetype_name, template_name, "template")
    if front_tmpl is None and back_tmpl is None:
        raise cmd.invalid("Provide at least one of --front or --back.")
    with cmd.errors(details={"notetype": name, "template": template}):
        return cmd.backend.edit_notetype_template(name, template, front=front_tmpl, back=back_tmpl)


@anki_command("notetype:css", errors=_MUTATION_ERRORS)
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--set", "css_value", default=None, help="Set CSS value")
def notetype_css_cmd(cmd: CommandContext, notetype_name: str, css_value: str | None) -> JSONValue:
    """Get or set CSS styling for a note type."""
    name = _require_name(cmd, notetype_name)
    with cmd.errors(details={"notetype": name}):
        if css_value is not None:
            return cmd.backend.set_notetype_css(name, css_value)
        item = cmd.backend.get_notetype(name)
        css = ""
        styling = item.get("styling")
        if isinstance(styling, dict):
            css = str(cast(dict[str, Any], styling).get("css") or "")
        return {"name": name, "css": css}
