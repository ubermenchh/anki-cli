from __future__ import annotations

import html as _html
import os
import re
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from anki_cli.cli.dispatcher import get_command, list_commands
from anki_cli.cli.params import preprocess_argv

_IN_REPL = False
_LOGO = r"""
               .....
              ........
             ...........
             ....-+-.....         .
             ....-+++.................
            .....+++++-................
         .......-++++++++---+++++-.....
     ..........-++++++++++++++++-....#
   .........-++++++++++++++++++-....+
  ......++++++++++++++++++++++.....#
  .......-+++++++++++++++++++-....#
   #-........-++++++++++++++++.....
      #-.......-+++++++++++++++.....
         #+....-++++++++++++++++.....
           .....++++++-.....---+-.....
           .....++++-.................
            ....++-.....-+-..........
            ..........-#     #######
            +.......-#
             #-...##
"""

_ALIASES: dict[str, str] = {
    "d": "decks",
    "dk": "deck",
    "dc": "deck:create",
    "dr": "deck:rename",
    "dd": "deck:delete",
    "n": "note",
    "na": "note:add",
    "nb": "note:bulk",
    "nt": "notetypes",
    "c": "cards",
    "t": "tags",
    "s": "search",
    "r": "review:next",
    "rs": "review:start",
    "ra": "review:answer",
    "rp": "review:preview",
    "ru": "review:undo",
    "v": "version",
}

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\\s*/?>")


def _strip_html(value: str) -> str:
    text = _BR_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _history_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    data_dir = base / "anki-cli"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "repl_history"


class _AnkiCompleter(Completer):

    def __init__(self) -> None:
        self._commands: list[str] = []
        self._options_cache: dict[str, list[str]] = {}
        self._builtins = ["help", "quit", "exit", "clear", "set"]

    def _ensure_commands(self) -> None:
        if not self._commands:
            self._commands = list_commands()

    def _options_for(self, name: str) -> list[str]:
        resolved = _ALIASES.get(name, name)
        if resolved in self._options_cache:
            return self._options_cache[resolved]
        cmd = get_command(resolved)
        if cmd is None:
            return []
        opts: list[str] = []
        for param in cmd.params:
            if isinstance(param, click.Option):
                opts.extend(param.opts)
                opts.extend(param.secondary_opts)
        self._options_cache[resolved] = opts
        return opts

    def _command_help(self, name: str) -> str:
        cmd = get_command(name)
        if cmd is not None and cmd.help:
            return cmd.help.strip().split("\n")[0][:50]
        return ""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        word = document.get_word_before_cursor(WORD=True)

        self._ensure_commands()

        if not words or (len(words) == 1 and not text.endswith(" ")):
            candidates = self._commands + list(_ALIASES.keys()) + self._builtins
            seen: set[str] = set()
            for c in candidates:
                if c in seen:
                    continue
                seen.add(c)
                if c.startswith(word):
                    target = _ALIASES.get(c)
                    meta = f"-> {target}" if target else self._command_help(c)
                    yield Completion(
                        c,
                        start_position=-len(word),
                        display_meta=meta,
                    )
        else:
            cmd_name = words[0]
            for opt in self._options_for(cmd_name):
                if opt.startswith(word):
                    yield Completion(opt, start_position=-len(word))


_STYLE = Style.from_dict({
    "prompt.arrow": "ansigreen bold",
    "bottom-toolbar": "bg:ansiblack ansiwhite",
    "bottom-toolbar.text": "",
})


def _invoke_command(ctx_obj: dict[str, Any], raw_args: list[str]) -> None:
    if not raw_args:
        return

    args = preprocess_argv(raw_args)
    cmd_name = _ALIASES.get(args[0], args[0])
    cmd_args = args[1:]

    cmd = get_command(cmd_name)
    if cmd is None:
        click.echo(f"Unknown command: {args[0]}  (try 'help')", err=True)
        return

    parent = click.Context(click.Group("anki"), obj=dict(ctx_obj))
    try:
        with parent:
            ctx = cmd.make_context(cmd_name, list(cmd_args), parent=parent)
            with ctx:
                cmd.invoke(ctx)
    except click.exceptions.Exit:
        pass
    except click.ClickException as exc:
        exc.show()
    except SystemExit:
        pass
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)


def _show_command_help(cmd_name: str) -> None:
    resolved = _ALIASES.get(cmd_name, cmd_name)
    cmd = get_command(resolved)
    if cmd is None:
        click.echo(f"Unknown command: {cmd_name}", err=True)
        return

    parent = click.Context(click.Group("anki"))
    with parent:
        ctx = click.Context(cmd, info_name=resolved, parent=parent)
        click.echo(cmd.get_help(ctx))


def _grouped_help() -> None:
    commands = list_commands()
    groups: dict[str, list[tuple[str, str]]] = {}
    for name in commands:
        prefix = name.split(":")[0] if ":" in name else "_general"
        cmd = get_command(name)
        desc = ""
        if cmd is not None and cmd.help:
            desc = cmd.help.strip().split("\n")[0][:55]
        groups.setdefault(prefix, []).append((name, desc))

    click.echo("")
    for group_name in sorted(groups.keys()):
        label = "General" if group_name == "_general" else group_name.capitalize()
        click.echo(click.style(f"  {label}", bold=True))
        for name, desc in groups[group_name]:
            padded = f"    {name:<24}"
            click.echo(f"{padded}{desc}")
        click.echo("")

    if _ALIASES:
        click.echo(click.style("  Aliases", bold=True))
        for alias, target in sorted(_ALIASES.items()):
            click.echo(f"    {alias:<24}{target}")
        click.echo("")

    click.echo(click.style("  Shell", bold=True))
    click.echo(f"    {'help [command]':<24}Show help")
    click.echo(f"    {'set format <fmt>':<24}Switch output format")
    click.echo(f"    {'clear':<24}Clear screen")
    click.echo(f"    {'quit':<24}Exit")
    click.echo("")


def _inline_review(ctx_obj: dict[str, Any], deck: str | None) -> None:
    from anki_cli.backends.factory import backend_session_from_context
    from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms

    try:
        backend_ctx = backend_session_from_context(ctx_obj)
        backend = backend_ctx.__enter__()
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        return

    undo = UndoStore()
    reviewed = 0

    try:
        while True:
            card_id: int | None = None
            kind = "none"

            if getattr(backend, "name", "") == "direct" and hasattr(backend, "_store"):
                store = cast(Any, backend._store)
                if hasattr(store, "get_next_due_card"):
                    picked = store.get_next_due_card(deck)
                    card_id = picked.get("card_id") if isinstance(picked, dict) else None
                    kind = str(picked.get("kind", "none")) if isinstance(picked, dict) else "none"

            if card_id is None:
                from anki_cli.core.scheduler import pick_next_due_card_id
                card_id, kind = pick_next_due_card_id(backend, deck=deck)

            if card_id is None:
                click.echo(
                    f"\n  No more due cards."
                    f"  deck={deck or '(all)'}  reviewed={reviewed}\n"
                )
                break

            rendered = _render_card_inline(backend, card_id)
            if rendered is None:
                click.echo("  (render failed, skipping)", err=True)
                continue

            question, answer = rendered

            click.echo("")
            click.echo(click.style("  Q: ", bold=True) + question)
            click.echo(click.style(f"  [{kind}]  card={card_id}", dim=True))

            try:
                input(click.style("  press enter to reveal...", dim=True))
            except (EOFError, KeyboardInterrupt):
                click.echo("")
                break

            click.echo(click.style("  A: ", bold=True) + answer)
            click.echo("")
            click.echo("  1=again  2=hard  3=good  4=easy  u=undo  q=stop")

            while True:
                try:
                    choice = input(click.style("  rate> ", fg="green", bold=True)).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "q"

                if choice == "q":
                    click.echo(f"\n  Stopped. reviewed={reviewed}\n")
                    return
                if choice == "u":
                    if getattr(backend, "name", "") == "direct" and hasattr(backend, "_store"):
                        col = getattr(backend, "collection_path", None)
                        collection = str(col) if col is not None else ""
                        item = undo.pop(collection=collection)
                        if item is None:
                            click.echo("  (nothing to undo)")
                            continue
                        try:
                            cast(Any, backend._store).restore_card_state(item.snapshot)
                            reviewed = max(0, reviewed - 1)
                            click.echo("  (undone)")
                        except Exception as exc:
                            click.echo(f"  undo failed: {exc}", err=True)
                    else:
                        click.echo("  (undo only available for direct backend)")
                    continue

                ease_map = {"1": 1, "2": 2, "3": 3, "4": 4,
                            "again": 1, "hard": 2, "good": 3, "easy": 4}
                ease = ease_map.get(choice)
                if ease is None:
                    click.echo("  (1/2/3/4/u/q)")
                    continue

                if getattr(backend, "name", "") == "direct" and hasattr(backend, "_store"):
                    col = getattr(backend, "collection_path", None)
                    collection = str(col) if col is not None else ""
                    snap = cast(Any, backend._store).snapshot_card_state(int(card_id))
                    undo.push(UndoItem(
                        collection=collection,
                        card_id=int(card_id),
                        snapshot=cast(dict[str, Any], snap),
                        created_at_epoch_ms=now_epoch_ms(),
                    ))

                try:
                    backend.answer_card(card_id=int(card_id), ease=ease)
                    reviewed += 1
                    click.echo(click.style(f"  rated {ease}  (reviewed={reviewed})", dim=True))
                except Exception as exc:
                    click.echo(f"  answer failed: {exc}", err=True)
                break

    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _render_card_inline(backend: Any, card_id: int) -> tuple[str, str] | None:
    from anki_cli.core.template import render_template

    card_obj = backend.get_card(card_id)
    card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}

    note_id: int | None = None
    for key in ("note", "nid", "noteId", "note_id"):
        v = card_map.get(key)
        if isinstance(v, int):
            note_id = v
            break
    if note_id is None:
        return None

    ord_ = int(card_map.get("ord", 0)) if isinstance(card_map.get("ord"), int) else 0
    fields_map = backend.get_note_fields(note_id=note_id, fields=None)

    notetype_name: str | None = None
    raw_nt = card_map.get("notetype_name")
    if isinstance(raw_nt, str) and raw_nt.strip():
        notetype_name = raw_nt.strip()
    else:
        note_obj = backend.get_note(note_id)
        if isinstance(note_obj, Mapping) and isinstance(note_obj.get("modelName"), str):
            notetype_name = str(note_obj["modelName"]).strip()
    if not notetype_name:
        return None

    nt_detail = backend.get_notetype(notetype_name)
    kind = str(nt_detail.get("kind", "normal")).lower()

    templates_raw = nt_detail.get("templates")
    templates_map: Mapping[str, Any]
    if isinstance(templates_raw, Mapping):
        templates_map = cast(Mapping[str, Any], templates_raw)
    else:
        templates_map = {}
    items = list(templates_map.items())

    tpl: Mapping[str, Any] | None = None
    for _name, t in items:
        if isinstance(t, Mapping) and isinstance(t.get("ord"), int) and t["ord"] == ord_:
            tpl = cast(Mapping[str, Any], t)
            break
    if tpl is None and 0 <= ord_ < len(items):
        _name, t = items[ord_]
        tpl = t if isinstance(t, Mapping) else {}
    if tpl is None and items:
        _name, t = items[0]
        tpl = t if isinstance(t, Mapping) else {}
    if tpl is None:
        return None

    front_tmpl = str(tpl.get("Front") or "")
    back_tmpl = str(tpl.get("Back") or "")

    if kind == "cloze":
        cloze_index = ord_ + 1
        question = render_template(
            front_tmpl, fields_map, cloze_index=cloze_index, reveal_cloze=False,
        )
        answer = render_template(
            back_tmpl, fields_map, front_side=question,
            cloze_index=cloze_index, reveal_cloze=True,
        )
    else:
        question = render_template(front_tmpl, fields_map)
        answer = render_template(back_tmpl, fields_map, front_side=question)

    return _strip_html(question), _strip_html(answer)


def run_repl(ctx_obj: dict[str, Any]) -> None:
    global _IN_REPL
    if _IN_REPL:
        click.echo("Already in interactive shell.", err=True)
        return
    _IN_REPL = True

    try:
        ctx_obj = dict(ctx_obj)
        ctx_obj["format"] = "table"

        last_cmd_ms: float | None = None

        def _toolbar() -> HTML:
            backend = ctx_obj.get("backend", "?")
            fmt = ctx_obj.get("format", "table")
            parts = [
                f"<b>{backend}</b>",
                f"format={fmt}",
            ]
            if last_cmd_ms is not None:
                parts.append(f"{last_cmd_ms:.0f}ms")
            return HTML("  ".join(parts))

        inner_completer = _AnkiCompleter()

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(_history_path())),
            completer=FuzzyCompleter(inner_completer),
            style=_STYLE,
            complete_while_typing=False,
            bottom_toolbar=_toolbar,
        )

        backend = ctx_obj.get("backend", "?")

        logo_lines = _LOGO.strip().splitlines()
        info_lines = [
            "",
            "anki-cli 0.1.0",
            "",
            f"{backend} backend",
            "",
            "Tab to complete",
            "Ctrl+R to search",
            "Ctrl+D to quit",
        ]

        max_logo_width = max(len(ln) for ln in logo_lines)
        pad = max_logo_width + 4
        for i in range(max(len(logo_lines), len(info_lines))):
            left = logo_lines[i] if i < len(logo_lines) else ""
            right = info_lines[i] if i < len(info_lines) else ""
            click.echo(f"{left:<{pad}}{right}")
        click.echo("")

        while True:
            try:
                line = session.prompt([
                    ("class:prompt.arrow", "> "),
                ])
            except (EOFError, KeyboardInterrupt):
                click.echo("")
                break

            stripped = line.strip()
            if not stripped:
                continue

            if stripped in {"quit", "exit", ":q"}:
                break

            if stripped.startswith("help"):
                rest = stripped[4:].strip()
                if rest:
                    _show_command_help(rest)
                else:
                    _grouped_help()
                continue

            if stripped in {"?", ":help"}:
                _grouped_help()
                continue

            if stripped in {"clear", ":clear"}:
                click.clear()
                continue

            if stripped.startswith("set format ") or stripped.startswith(":set format "):
                fmt = stripped.split("format", 1)[1].strip().lower()
                if fmt in {"table", "json", "md", "csv", "plain"}:
                    ctx_obj["format"] = fmt
                    click.echo(f"  format -> {fmt}")
                else:
                    click.echo("  usage: set format table|json|md|csv|plain", err=True)
                continue

            if stripped.startswith("review ") or stripped == "review":
                parts = stripped.split(None, 1)
                if len(parts) == 1 or parts[1] in {"start", "inline"}:
                    _inline_review(ctx_obj, deck=None)
                    continue
                if parts[1].startswith("start ") or parts[1].startswith("inline "):
                    deck_arg = parts[1].split(None, 1)
                    deck = deck_arg[1].strip() if len(deck_arg) > 1 else None
                    _inline_review(ctx_obj, deck=deck)
                    continue

            try:
                parts = shlex.split(stripped)
            except ValueError as exc:
                click.echo(f"Parse error: {exc}", err=True)
                continue

            t0 = time.monotonic()
            _invoke_command(ctx_obj, parts)
            last_cmd_ms = (time.monotonic() - t0) * 1000

    finally:
        _IN_REPL = False