from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click
from markdownify import markdownify
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from anki_cli.cli.dispatcher import get_command, list_commands
from anki_cli.cli.params import preprocess_argv

from .colors import (
    BLUE,
    CYAN,
    DIM,
    GREEN,
    MENU_BG,
    RED,
    SELECTION_BG,
    TEXT,
    TOOLBAR_BG,
)

console = Console()

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
    return markdownify(value).strip()


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
        self._builtins = [
            "help", "quit", "exit", "clear", "set", "use",
        ]

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
            candidates = (
                self._commands + list(_ALIASES.keys()) + self._builtins
            )
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
    "prompt.arrow": f"{CYAN} bold",
    "bottom-toolbar": f"bg:{TOOLBAR_BG} {TEXT}",
    "bottom-toolbar.text": "",
    "completion-menu.completion": f"bg:{MENU_BG} {TEXT}",
    "completion-menu.completion.current": f"bg:{SELECTION_BG} {TEXT} bold",
    "completion-menu.meta.completion": f"bg:{MENU_BG} {DIM}",
    "completion-menu.meta.completion.current": f"bg:{SELECTION_BG} {TEXT}",
    "scrollbar.background": f"bg:{TOOLBAR_BG}",
    "scrollbar.button": f"bg:{DIM}",
})


def _invoke_command(ctx_obj: dict[str, Any], raw_args: list[str]) -> None:
    if not raw_args:
        return

    args = preprocess_argv(raw_args)
    cmd_name = _ALIASES.get(args[0], args[0])
    cmd_args = args[1:]

    cmd = get_command(cmd_name)
    if cmd is None:
        click.echo(
            f"Unknown command: {args[0]}  (try 'help')", err=True
        )
        return

    parent = click.Context(click.Group("anki"), obj=dict(ctx_obj))
    try:
        with parent:
            ctx = cmd.make_context(
                cmd_name, list(cmd_args), parent=parent
            )
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

    console.print()
    for group_name in sorted(groups.keys()):
        label = (
            "General" if group_name == "_general"
            else group_name.capitalize()
        )
        console.print(f"  [bold {BLUE}]{label}[/]")
        
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Command", style=CYAN, width=24)
        table.add_column("Description", style=DIM)
        
        for name, desc in groups[group_name]:
            table.add_row(f"    {name}", desc)
            
        console.print(table)
        console.print()

    if _ALIASES:
        console.print(f"  [bold {BLUE}]Aliases[/]")
        alias_table = Table(show_header=False, box=None, padding=(0, 2))
        alias_table.add_column("Alias", style=CYAN, width=24)
        alias_table.add_column("Target", style=DIM)
        for alias, target in sorted(_ALIASES.items()):
            alias_table.add_row(f"    {alias}", target)
        console.print(alias_table)
        console.print()

    console.print(f"  [bold {BLUE}]Shell[/]")
    shell_table = Table(show_header=False, box=None, padding=(0, 2))
    shell_table.add_column("Command", style=CYAN, width=24)
    shell_table.add_column("Description", style=DIM)
    shell_table.add_row("    help [command]", "Show help")
    shell_table.add_row("    use <deck>", "Set default deck context")
    shell_table.add_row("    use", "Clear deck context")
    shell_table.add_row("    set format <fmt>", "Switch output format")
    shell_table.add_row("    !<cmd>", "Run a shell command")
    shell_table.add_row("    !!", "Repeat last command")
    shell_table.add_row("    clear", "Clear screen")
    shell_table.add_row("    quit", "Exit")
    console.print(shell_table)
    console.print()


def _fetch_due_counts(ctx_obj: dict[str, Any], deck: str | None) -> str:
    try:
        from anki_cli.backends.factory import backend_session_from_context
        with backend_session_from_context(ctx_obj) as backend:
            counts = backend.get_due_counts(
                deck=deck.strip() if deck else None
            )
            new = counts.get("new", 0)
            learn = counts.get("learn", 0)
            review = counts.get("review", 0)
            return f"new={new} learn={learn} review={review}"
    except Exception:
        return ""


def _inline_review(ctx_obj: dict[str, Any], deck: str | None) -> None:
    from anki_cli.backends.factory import backend_session_from_context
    from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms

    try:
        backend_ctx = backend_session_from_context(ctx_obj)
        backend = backend_ctx.__enter__()
    except Exception as exc:
        console.print(f"[#f7768e]Error:[/] {exc}")
        return

    undo = UndoStore()
    reviewed = 0

    try:
        while True:
            card_id: int | None = None
            kind = "none"

            if (
                getattr(backend, "name", "") == "direct"
                and hasattr(backend, "_store")
            ):
                store = cast(Any, backend._store)
                if hasattr(store, "get_next_due_card"):
                    picked = store.get_next_due_card(deck)
                    cid = picked.get("card_id") if isinstance(picked, dict) else None
                    card_id = int(cid) if isinstance(cid, int) else None
                    kind = (
                        str(picked.get("kind", "none"))
                        if isinstance(picked, dict) else "none"
                    )

            if card_id is None:
                from anki_cli.core.scheduler import pick_next_due_card_id
                card_id, kind = pick_next_due_card_id(
                    backend, deck=deck
                )

            if card_id is None:
                console.print(
                    f"\n  [{DIM}]No more due cards.[/]"
                    f"  [{DIM}]deck={deck or '(all)'}  reviewed={reviewed}[/]\n"
                )
                break

            rendered = _render_card_inline(backend, card_id)
            if rendered is None:
                console.print(f"  [{RED}](render failed, skipping)[/]")
                continue

            question, answer = rendered

            console.print()
            console.print(Panel(
                Markdown(question),
                title=f"[bold {CYAN}]Question[/] [{DIM}]({kind} card={card_id})[/]",
                border_style=CYAN,
                padding=(1, 2)
            ))

            try:
                # Use standard input styled nicely
                input("  press enter to reveal... ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            console.print(Panel(
                Markdown(answer),
                title=f"[bold {GREEN}]Answer[/]",
                border_style=GREEN,
                padding=(1, 2)
            ))
            
            console.print(f"  [{DIM}]1=again  2=hard  3=good  4=easy  u=undo  q=stop[/]")

            while True:
                try:
                    # Switch click prompt for rich console input for hex coloring
                    choice = console.input(f"  [bold {GREEN}]rate>[/] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "q"

                if choice == "q":
                    console.print(
                        f"\n  [{DIM}]Stopped. reviewed={reviewed}[/]\n"
                    )
                    return
                if choice == "u":
                    if (
                        getattr(backend, "name", "") == "direct"
                        and hasattr(backend, "_store")
                    ):
                        col = getattr(
                            backend, "collection_path", None
                        )
                        collection = str(col) if col is not None else ""
                        item = undo.pop(collection=collection)
                        if item is None:
                            console.print(f"  [{DIM}](nothing to undo)[/]")
                            continue
                        try:
                            cast(
                                Any, backend._store
                            ).restore_card_state(item.snapshot)
                            reviewed = max(0, reviewed - 1)
                            console.print(f"  [{DIM}](undone)[/]")
                        except Exception as exc:
                            console.print(
                                f"  [{RED}]undo failed:[/] {exc}"
                            )
                    else:
                        console.print(
                            f"  [{DIM}](undo only available for direct backend)[/]"
                        )
                    continue

                ease_map = {
                    "1": 1, "2": 2, "3": 3, "4": 4,
                    "again": 1, "hard": 2, "good": 3, "easy": 4,
                }
                ease = ease_map.get(choice)
                if ease is None:
                    console.print(f"  [{DIM}](1/2/3/4/u/q)[/]")
                    continue

                if (
                    getattr(backend, "name", "") == "direct"
                    and hasattr(backend, "_store")
                ):
                    col = getattr(backend, "collection_path", None)
                    collection = str(col) if col is not None else ""
                    snap = cast(
                        Any, backend._store
                    ).snapshot_card_state(int(card_id))
                    undo.push(UndoItem(
                        collection=collection,
                        card_id=int(card_id),
                        snapshot=cast(dict[str, Any], snap),
                        created_at_epoch_ms=now_epoch_ms(),
                    ))

                try:
                    backend.answer_card(
                        card_id=int(card_id), ease=ease
                    )
                    reviewed += 1
                    console.print(
                        f"  [{DIM}]rated {ease}  (reviewed={reviewed})[/]"
                    )
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    console.print(f"  [{RED}]answer failed:[/] {msg}")
                break

    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _render_card_inline(
    backend: Any, card_id: int
) -> tuple[str, str] | None:
    from anki_cli.core.template import render_template

    card_obj = backend.get_card(card_id)
    card_map = (
        cast(Mapping[str, Any], card_obj)
        if isinstance(card_obj, Mapping) else {}
    )

    note_id: int | None = None
    for key in ("note", "nid", "noteId", "note_id"):
        v = card_map.get(key)
        if isinstance(v, int):
            note_id = v
            break
    if note_id is None:
        return None

    raw_ord = card_map.get("ord")
    ord_ = int(raw_ord) if isinstance(raw_ord, int) else 0
    fields_map = backend.get_note_fields(note_id=note_id, fields=None)

    notetype_name: str | None = None
    raw_nt = card_map.get("notetype_name")
    if isinstance(raw_nt, str) and raw_nt.strip():
        notetype_name = raw_nt.strip()
    else:
        note_obj = backend.get_note(note_id)
        if (
            isinstance(note_obj, Mapping)
            and isinstance(note_obj.get("modelName"), str)
        ):
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
        if (
            isinstance(t, Mapping)
            and isinstance(t.get("ord"), int)
            and t["ord"] == ord_
        ):
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
            front_tmpl, fields_map,
            cloze_index=cloze_index, reveal_cloze=False,
        )
        answer = render_template(
            back_tmpl, fields_map, front_side=question,
            cloze_index=cloze_index, reveal_cloze=True,
        )
    else:
        question = render_template(front_tmpl, fields_map)
        answer = render_template(
            back_tmpl, fields_map, front_side=question
        )

    return _strip_html(question), _strip_html(answer)


def run_repl(ctx_obj: dict[str, Any]) -> None:
    global _IN_REPL
    if _IN_REPL:
        console.print(f"[{RED}]Already in interactive shell.[/]")
        return
    _IN_REPL = True

    try:
        ctx_obj = dict(ctx_obj)
        ctx_obj["format"] = "table"

        last_cmd_ms: float | None = None
        last_command: str | None = None
        deck_context: str | None = None
        due_status: str = ""

        def _refresh_due() -> None:
            nonlocal due_status
            due_status = _fetch_due_counts(ctx_obj, deck_context)

        _refresh_due()

        def _toolbar() -> HTML:
            backend = ctx_obj.get("backend", "?")
            fmt = ctx_obj.get("format", "table")
            parts = [f"<b>{backend}</b>", f"format={fmt}"]
            if deck_context:
                parts.append(f"deck={deck_context}")
            if due_status:
                parts.append(due_status)
            if last_cmd_ms is not None:
                parts.append(f"{last_cmd_ms:.0f}ms")
            return HTML("  ".join(parts))

        inner_completer = _AnkiCompleter()

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(_history_path())),
            completer=FuzzyCompleter(inner_completer),
            style=_STYLE,
            complete_while_typing=True,
            bottom_toolbar=_toolbar,
            auto_suggest=AutoSuggestFromHistory(),
        )

        backend = ctx_obj.get("backend", "?")

        header_table = Table.grid(padding=(0, 4))
        header_table.add_column("Logo")
        header_table.add_column("Title", vertical="middle")

        logo_text = Text(_LOGO.strip("\n"))
        logo_text.stylize(f"bold {BLUE}")

        title_text = Text(f"anki-cli 0.1.0\n", style=f"bold {BLUE}")
        title_text.append(f"{backend} backend", style=DIM)

        header_table.add_row(logo_text, title_text)

        console.print()
        console.print(header_table)
        console.print()
        console.print(f"  [bold {TEXT}]Interactive Shell[/]")
        console.print(f"  [{DIM}]Tab to autocomplete, ↑/↓ for history, Ctrl+D to quit[/]")
        console.print()
        console.print(Rule(style=DIM))

        while True:
            try:
                line = session.prompt([
                    ("class:prompt.arrow", "> "),
                ])
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            stripped = line.strip()
            if not stripped:
                continue

            # !! repeats last command
            if stripped == "!!" and last_command:
                stripped = last_command
                console.print(f"  [{DIM}]>> {stripped}[/]")

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

            # ! shell escape
            if stripped.startswith("!") and stripped != "!!":
                shell_cmd = stripped[1:].strip()
                if shell_cmd:
                    try:
                        subprocess.run(
                            shell_cmd, shell=True, check=False
                        )
                    except Exception as exc:
                        console.print(f"  [{RED}]shell error:[/] {exc}")
                continue

            # use <deck> / use (clear)
            if stripped == "use" or stripped.startswith("use "):
                rest = stripped[3:].strip()
                if rest:
                    deck_context = rest
                    console.print(f"  [{DIM}]deck context -> {deck_context}[/]")
                else:
                    deck_context = None
                    console.print(f"  [{DIM}]deck context cleared[/]")
                _refresh_due()
                continue

            if (
                stripped.startswith("set format ")
                or stripped.startswith(":set format ")
            ):
                fmt = stripped.split("format", 1)[1].strip().lower()
                if fmt in {"table", "json", "md", "csv", "plain"}:
                    ctx_obj["format"] = fmt
                    console.print(f"  [{DIM}]format -> {fmt}[/]")
                else:
                    console.print(
                        f"  [{RED}]usage: set format table|json|md|csv|plain[/]"
                    )
                continue

            if stripped.startswith("review ") or stripped == "review":
                parts = stripped.split(None, 1)
                tail = parts[1] if len(parts) > 1 else ""
                if not tail or tail in {"start", "inline"}:
                    _inline_review(ctx_obj, deck=deck_context)
                    _refresh_due()
                    last_command = stripped
                    continue
                if tail.startswith("start ") or tail.startswith("inline "):
                    deck_arg = tail.split(None, 1)
                    deck = (
                        deck_arg[1].strip() if len(deck_arg) > 1
                        else deck_context
                    )
                    _inline_review(ctx_obj, deck=deck)
                    _refresh_due()
                    last_command = stripped
                    continue

            try:
                parts = shlex.split(stripped)
            except ValueError as exc:
                console.print(f"[{RED}]Parse error:[/] {exc}")
                continue

            # Inject --deck from context if command supports it
            # and user didn't explicitly provide one
            if (
                deck_context
                and "--deck" not in parts
                and len(parts) >= 1
            ):
                resolved = _ALIASES.get(parts[0], parts[0])
                cmd = get_command(resolved)
                if cmd is not None:
                    deck_params = [
                        p for p in cmd.params
                        if isinstance(p, click.Option)
                        and "--deck" in p.opts
                    ]
                    if deck_params:
                        parts.extend(["--deck", deck_context])

            last_command = stripped
            t0 = time.monotonic()
            
            with console.status(f"[{DIM}]Running...[/]", spinner="dots"):
                _invoke_command(ctx_obj, parts)
                
            last_cmd_ms = (time.monotonic() - t0) * 1000
            _refresh_due()

    finally:
        _IN_REPL = False