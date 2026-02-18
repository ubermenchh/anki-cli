from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click
from pydantic import BaseModel
from rich.box import SIMPLE_HEAD
from rich.console import Console
from rich.table import Table

from anki_cli.models.output import (
    ErrorInfo,
    ErrorResponse,
    JSONValue,
    Meta,
    SuccessResponse,
)


class OutputFormatter:
    def __init__(
        self,
        *,
        output_format: str,
        backend: str,
        collection_path: str | None,
        no_color: bool,
        copy_output: bool,
    ) -> None:
        self.output_format = output_format.lower()
        self.backend = backend
        self.collection_path = collection_path
        self.no_color = no_color
        self.copy_output = copy_output

    def emit_success(self, *, command: str, data: JSONValue | BaseModel) -> None:
        normalized = self._normalize_data(data)

        response = SuccessResponse(
            data=normalized,
            meta=self._build_meta(command),
        )

        if self.output_format == "json":
            text = json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2)
            click.echo(text)
            self._copy_if_requested(text)
            return

        rendered = self._render_data(normalized)
        click.echo(rendered)
        self._copy_if_requested(rendered)

    def emit_error(
        self,
        *,
        command: str,
        code: str,
        message: str,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        payload = ErrorResponse(
            error=ErrorInfo(
                code=code,
                message=message,
                details=details or {},
            ),
            meta=self._build_meta(command),
        )

        if self.output_format == "json":
            text = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2)
            click.echo(text, err=True)
            self._copy_if_requested(text)
            return

        click.echo(f"{code}: {message}", err=True)
        if payload.error.details:
            for key, value in payload.error.details.items():
                click.echo(f"- {key}: {self._stringify(value)}", err=True)

    def _build_meta(self, command: str) -> Meta:
        timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        )
        return Meta(
            command=command,
            backend=self.backend,
            collection=self.collection_path,
            timestamp=timestamp,
        )

    def _normalize_data(self, data: JSONValue | BaseModel) -> JSONValue:
        if isinstance(data, BaseModel):
            dumped = data.model_dump(mode="json")
            return dumped
        return data

    def _render_data(self, data: JSONValue) -> str:
        if self.output_format == "table":
            return self._render_table(data)
        if self.output_format == "md":
            return self._render_md(data)
        if self.output_format == "csv":
            return self._render_csv(data)
        if self.output_format == "plain":
            return self._render_plain(data)
        return self._render_plain(data)

    def _render_table(self, data: JSONValue) -> str:
        rows, columns = self._coerce_rows(data)
        if not rows:
            return "(no data)"

        display_columns: list[str] = []
        for col in columns:
            sample = next(
                (row.get(col) for row in rows if row.get(col) is not None),
                None,
            )
            if isinstance(sample, (dict, list)):
                continue
            display_columns.append(col)

        if not display_columns:
            display_columns = columns

        display_columns = [
            col for col in display_columns
            if any(
                row.get(col) is not None and row.get(col) != ""
                for row in rows
            )
        ]

        if not display_columns:
            display_columns = columns

        table = Table(
            box=SIMPLE_HEAD,
            pad_edge=False,
            show_edge=False,
            row_styles=["", "dim"],
        )
        for column in display_columns:
            is_numeric = all(
                isinstance(row.get(column), (int, float))
                for row in rows
                if row.get(column) is not None and row.get(column) != ""
            )
            table.add_column(
                column,
                justify="right" if is_numeric else "left",
                no_wrap=True,
            )

        for row in rows:
            table.add_row(
                *[self._stringify(row.get(column)) for column in display_columns]
            )

        console = Console(
            record=True,
            no_color=self.no_color,
            force_terminal=False,
            file=io.StringIO(),
        )
        console.print(table)
        return console.export_text().rstrip()

    def _render_md(self, data: JSONValue) -> str:
        rows, columns = self._coerce_rows(data)
        if not rows:
            return "(no data)"

        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join(["---"] * len(columns)) + " |"
        lines = [header, divider]

        for row in rows:
            values = [self._escape_md(self._stringify(row.get(column))) for column in columns]
            lines.append("| " + " | ".join(values) + " |")

        return "\n".join(lines)

    def _render_csv(self, data: JSONValue) -> str:
        rows, columns = self._coerce_rows(data)
        if not rows:
            return ""

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: self._stringify(row.get(column)) for column in columns})
        return buffer.getvalue().rstrip("\n")

    def _render_plain(self, data: JSONValue) -> str:
        rows, columns = self._coerce_rows(data)
        if not rows:
            return ""

        if len(rows) == 1 and len(columns) > 1:
            single = rows[0]
            return "\n".join(
                f"{column}={self._stringify(single.get(column))}" for column in columns
            )

        lines: list[str] = []
        for row in rows:
            parts = [f"{column}={self._stringify(row.get(column))}" for column in columns]
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def _coerce_rows(self, data: JSONValue) -> tuple[list[dict[str, JSONValue]], list[str]]:
        if isinstance(data, dict):
            items_val = cast(dict[str, Any], data).get("items")
            if isinstance(items_val, list) and items_val and isinstance(items_val[0], dict):
                dict_rows = [
                    {str(k): cast(JSONValue, v) for k, v in item.items()}
                    for item in items_val
                    if isinstance(item, dict)
                ]
                columns = self._ordered_union(dict_rows)
                return dict_rows, columns

            row = {str(key): cast(JSONValue, value) for key, value in data.items()}
            return [row], list(row.keys())

        if isinstance(data, list):
            if not data:
                return [], []

            dict_rows: list[dict[str, JSONValue]] = []
            for item in data:
                if not isinstance(item, dict):
                    rows = [{"value": value} for value in data]
                    return rows, ["value"]
                dict_rows.append({str(k): cast(JSONValue, v) for k, v in item.items()})

            columns = self._ordered_union(dict_rows)
            return dict_rows, columns

        return [{"value": data}], ["value"]

    def _ordered_union(self, rows: list[dict[str, JSONValue]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for row in rows:
            for key in row:
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(key)
        return ordered

    def _stringify(self, value: JSONValue | None) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _escape_md(self, value: str) -> str:
        return value.replace("|", "\\|").replace("\n", "<br>")

    def _copy_if_requested(self, text: str) -> None:
        if not self.copy_output:
            return

        try:
            import pyperclip
        except ModuleNotFoundError:
            click.echo(
                "warning: --copy requested but optional clipboard dependency is not installed",
                err=True,
            )
            return

        try:
            pyperclip.copy(text)
        except Exception:
            click.echo("warning: clipboard is unavailable on this system", err=True)


def formatter_from_ctx(ctx: click.Context) -> OutputFormatter:
    obj: dict[str, Any] = ctx.obj or {}

    collection_path: str | None
    raw_collection = obj.get("collection_path")
    if isinstance(raw_collection, Path):
        collection_path = str(raw_collection)
    elif raw_collection is None:
        collection_path = None
    else:
        collection_path = str(raw_collection)

    return OutputFormatter(
        output_format=str(obj.get("format", "table")),
        backend=str(obj.get("backend", "none")),
        collection_path=collection_path,
        no_color=bool(obj.get("no_color", False)),
        copy_output=bool(obj.get("copy", False)),
    )