from __future__ import annotations

from typing import Any

import click

from anki_cli.backends.ankiconnect import AnkiConnectAPIError
from anki_cli.backends.factory import (
    BackendFactoryError,
    BackendNotImplementedError,
    backend_session_from_context,
)
from anki_cli.cli.dispatcher import register_command
from anki_cli.cli.formatter import formatter_from_ctx


def _emit_backend_unavailable(
    *,
    ctx: click.Context,
    command: str,
    obj: dict[str, Any],
    error: Exception,
) -> None:
    formatter = formatter_from_ctx(ctx)
    formatter.emit_error(
        command=command,
        code="BACKEND_UNAVAILABLE",
        message=str(error),
        details={"backend": str(obj.get("backend", "unknown"))},
    )
    raise click.exceptions.Exit(7) from error


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


@click.command("notetypes")
@click.pass_context
def notetypes_cmd(ctx: click.Context) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    try:
        with backend_session_from_context(obj) as backend:
            items = backend.get_notetypes()
    except (BackendNotImplementedError, BackendFactoryError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetypes", obj=obj, error=exc)

    formatter.emit_success(
        command="notetypes",
        data={"count": len(items), "items": items},
    )


@click.command("notetype")
@click.option(
    "--notetype",
    "--name",
    "notetype_name",
    required=True,
    help="Notetype name, e.g. Basic",
)
@click.pass_context
def notetype_cmd(ctx: click.Context, notetype_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)

    normalized_name = notetype_name.strip()
    if not normalized_name:
        formatter.emit_error(
            command="notetype",
            code="INVALID_INPUT",
            message="Notetype name cannot be empty.",
            details={"notetype": notetype_name},
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            item = backend.get_notetype(normalized_name)
    except (BackendNotImplementedError, BackendFactoryError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError) as exc:
        formatter.emit_error(
            command="notetype",
            code="ENTITY_NOT_FOUND",
            message=str(exc),
            details={"notetype": normalized_name},
        )
        raise click.exceptions.Exit(4) from exc

    formatter.emit_success(command="notetype", data=item)


@click.command("notetype:create")
@click.option("--name", "notetype_name", required=True, help="New notetype name")
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
@click.pass_context
def notetype_create_cmd(
    ctx: click.Context,
    notetype_name: str,
    kind: str,
    fields: tuple[str, ...],
    template_name: str | None,
    front_tmpl: str | None,
    back_tmpl: str | None,
    css: str,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()
    normalized_kind = kind.strip().lower()

    if not normalized_name:
        formatter.emit_error(
            command="notetype:create",
            code="INVALID_INPUT",
            message="Notetype name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    cleaned_fields = [field.strip() for field in fields if field.strip()]
    if not cleaned_fields:
        cleaned_fields = ["Text", "Extra"] if normalized_kind == "cloze" else ["Front", "Back"]

    default_name, default_front, default_back = _default_templates(normalized_kind)
    template = template_name.strip() if template_name else default_name
    front = front_tmpl if front_tmpl is not None else default_front
    back = back_tmpl if back_tmpl is not None else default_back

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.create_notetype(
                name=normalized_name,
                fields=cleaned_fields,
                templates=[{"name": template, "front": front, "back": back}],
                css=css,
                kind=normalized_kind,
            )
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:create", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:create",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:create", data=data)


@click.command("notetype:field:add")
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--field", "field_name", required=True, help="Field name")
@click.pass_context
def notetype_field_add_cmd(ctx: click.Context, notetype_name: str, field_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()
    normalized_field = field_name.strip()

    if not normalized_name or not normalized_field:
        formatter.emit_error(
            command="notetype:field:add",
            code="INVALID_INPUT",
            message="Both --notetype and --field are required.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.add_notetype_field(normalized_name, normalized_field)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:field:add", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:field:add",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name, "field": normalized_field},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:field:add", data=data)


@click.command("notetype:field:remove")
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--field", "field_name", required=True, help="Field name")
@click.pass_context
def notetype_field_remove_cmd(ctx: click.Context, notetype_name: str, field_name: str) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()
    normalized_field = field_name.strip()

    if not normalized_name or not normalized_field:
        formatter.emit_error(
            command="notetype:field:remove",
            code="INVALID_INPUT",
            message="Both --notetype and --field are required.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.remove_notetype_field(normalized_name, normalized_field)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:field:remove", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:field:remove",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name, "field": normalized_field},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:field:remove", data=data)


@click.command("notetype:template:add")
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--template", "template_name", required=True, help="Template name")
@click.option("--front", "front_tmpl", required=True, help="Front template")
@click.option("--back", "back_tmpl", required=True, help="Back template")
@click.pass_context
def notetype_template_add_cmd(
    ctx: click.Context,
    notetype_name: str,
    template_name: str,
    front_tmpl: str,
    back_tmpl: str,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()
    normalized_template = template_name.strip()

    if not normalized_name or not normalized_template:
        formatter.emit_error(
            command="notetype:template:add",
            code="INVALID_INPUT",
            message="Both --notetype and --template are required.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.add_notetype_template(
                normalized_name,
                normalized_template,
                front_tmpl,
                back_tmpl,
            )
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:template:add", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:template:add",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name, "template": normalized_template},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:template:add", data=data)


@click.command("notetype:template:edit")
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--template", "template_name", required=True, help="Template name")
@click.option("--front", "front_tmpl", default=None, help="New front template")
@click.option("--back", "back_tmpl", default=None, help="New back template")
@click.pass_context
def notetype_template_edit_cmd(
    ctx: click.Context,
    notetype_name: str,
    template_name: str,
    front_tmpl: str | None,
    back_tmpl: str | None,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()
    normalized_template = template_name.strip()

    if not normalized_name or not normalized_template:
        formatter.emit_error(
            command="notetype:template:edit",
            code="INVALID_INPUT",
            message="Both --notetype and --template are required.",
        )
        raise click.exceptions.Exit(2)
    if front_tmpl is None and back_tmpl is None:
        formatter.emit_error(
            command="notetype:template:edit",
            code="INVALID_INPUT",
            message="Provide at least one of --front or --back.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            data = backend.edit_notetype_template(
                normalized_name,
                normalized_template,
                front=front_tmpl,
                back=back_tmpl,
            )
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:template:edit", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:template:edit",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name, "template": normalized_template},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:template:edit", data=data)


@click.command("notetype:css")
@click.option("--notetype", "notetype_name", required=True, help="Notetype name")
@click.option("--set", "css_value", default=None, help="Set CSS value")
@click.pass_context
def notetype_css_cmd(
    ctx: click.Context,
    notetype_name: str,
    css_value: str | None,
) -> None:
    obj: dict[str, Any] = ctx.obj or {}
    formatter = formatter_from_ctx(ctx)
    normalized_name = notetype_name.strip()

    if not normalized_name:
        formatter.emit_error(
            command="notetype:css",
            code="INVALID_INPUT",
            message="Notetype name cannot be empty.",
        )
        raise click.exceptions.Exit(2)

    try:
        with backend_session_from_context(obj) as backend:
            if css_value is None:
                item = backend.get_notetype(normalized_name)
                css = ""
                styling = item.get("styling")
                if isinstance(styling, dict):
                    css = str(styling.get("css", ""))
                data: dict[str, Any] = {"name": normalized_name, "css": css}
            else:
                data = backend.set_notetype_css(normalized_name, css_value)
    except (BackendNotImplementedError, BackendFactoryError, NotImplementedError) as exc:
        _emit_backend_unavailable(ctx=ctx, command="notetype:css", obj=obj, error=exc)
    except (AnkiConnectAPIError, LookupError, ValueError) as exc:
        formatter.emit_error(
            command="notetype:css",
            code="BACKEND_OPERATION_FAILED",
            message=str(exc),
            details={"notetype": normalized_name},
        )
        raise click.exceptions.Exit(1) from exc

    formatter.emit_success(command="notetype:css", data=data)


register_command("notetypes", notetypes_cmd)
register_command("notetype", notetype_cmd)
register_command("notetype:create", notetype_create_cmd)
register_command("notetype:field:add", notetype_field_add_cmd)
register_command("notetype:field:remove", notetype_field_remove_cmd)
register_command("notetype:template:add", notetype_template_add_cmd)
register_command("notetype:template:edit", notetype_template_edit_cmd)
register_command("notetype:css", notetype_css_cmd)