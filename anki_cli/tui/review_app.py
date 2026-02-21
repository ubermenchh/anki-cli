from __future__ import annotations

import html as _html
import re
import shlex
from collections.abc import Mapping
from typing import Any, ClassVar, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static

from anki_cli.core.scheduler import pick_next_due_card_id
from anki_cli.core.template import render_template
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_HR_RE = re.compile(r"(?i)<hr[^>]*>")


def _strip_html_basic(value: str) -> str:
    # Good enough for terminal UI: keep line breaks, drop tags.
    text = _BR_RE.sub("\n", value)
    text = _HR_RE.sub("\n" + ("-" * 40) + "\n", text)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _extract_note_id(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        v = card.get(key)
        if isinstance(v, int):
            return v
    return None


def _extract_ord(card: Mapping[str, Any]) -> int:
    v = card.get("ord")
    return int(v) if isinstance(v, int) else 0


def _pick_template(templates: Mapping[str, Any], ord_: int) -> Mapping[str, Any] | None:
    items = list(templates.items())

    # Prefer explicit ord (direct backend provides it).
    for _name, tmpl in items:
        if isinstance(tmpl, Mapping) and isinstance(tmpl.get("ord"), int) and tmpl["ord"] == ord_:
            return cast(Mapping[str, Any], tmpl)

    # Fallback: index into insertion order.
    if 0 <= ord_ < len(items):
        _name, tmpl = items[ord_]
        return tmpl if isinstance(tmpl, Mapping) else {}

    if items:
        _name, tmpl = items[0]
        return tmpl if isinstance(tmpl, Mapping) else {}

    return None


class PreviewScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "app.pop_screen", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Static(self._title)
        with VerticalScroll():
            yield Static(self._body)


class ReviewApp(App[None]):
    CSS = """
    Screen { align: center middle; }
    #main { width: 100%; height: 1fr; }
    #status { height: auto; padding: 0 1; }
    #card { height: 1fr; padding: 0 1; }
    #cmd { height: auto; padding: 0 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding(":", "focus_command", "Command"),
        Binding("escape", "blur_command", "Blur command"),
        Binding("space", "toggle_answer", "Show/Hide answer"),
        Binding("1", "rate(1)", "Again"),
        Binding("2", "rate(2)", "Hard"),
        Binding("3", "rate(3)", "Good"),
        Binding("4", "rate(4)", "Easy"),
        Binding("u", "undo", "Undo"),
        Binding("p", "preview", "Preview"),
        Binding("n", "next", "Next"),
    ]

    def __init__(self, *, backend: Any, deck: str | None) -> None:
        super().__init__()
        self._backend = backend
        self._deck = deck
        self._card_id: int | None = None
        self._kind: str = "none"
        self._show_answer = False
        self._undo = UndoStore()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
        yield Static("", id="status")
        with VerticalScroll(id="main"):
            yield Static("", id="card")
        yield Input(placeholder=":help", id="cmd")

    def on_mount(self) -> None:
        self._load_next()

    async def action_quit(self) -> None:
        self.exit()

    def action_toggle_answer(self) -> None:
        if self._card_id is None:
            return
        self._show_answer = not self._show_answer
        self._render_current()

    def action_next(self) -> None:
        self._load_next()

    def action_rate(self, ease: int) -> None:
        if self._card_id is None:
            return

        # Require showing the answer before rating (closer to Anki UX).
        if not self._show_answer:
            self._show_answer = True
            self._set_status("Answer shown. Press 1-4 to rate.")
            self._render_current()
            return

        # Save undo snapshot (direct backend only).
        if getattr(self._backend, "name", "") == "direct" and hasattr(self._backend, "_store"):
            col = getattr(self._backend, "collection_path", None)
            collection = str(col) if col is not None else ""
            store = cast(Any, self._backend._store)
            snap = store.snapshot_card_state(int(self._card_id))
            self._undo.push(
                UndoItem(
                    collection=collection,
                    card_id=int(self._card_id),
                    snapshot=cast(dict[str, Any], snap),
                    created_at_epoch_ms=now_epoch_ms(),
                )
            )

        try:
            self._backend.answer_card(card_id=int(self._card_id), ease=int(ease))
        except Exception as exc:
            self._set_status(f"answer failed: {exc}")
            return

        self._load_next()

    def action_undo(self) -> None:
        if getattr(self._backend, "name", "") != "direct" or not hasattr(self._backend, "_store"):
            self._set_status("undo is supported only for direct backend")
            return

        col = getattr(self._backend, "collection_path", None)
        collection = str(col) if col is not None else ""
        item = self._undo.pop(collection=collection)
        if item is None:
            self._set_status("undo empty")
            return

        store = cast(Any, self._backend._store)
        try:
            store.restore_card_state(item.snapshot)
        except Exception as exc:
            self._set_status(f"undo failed: {exc}")
            return

        self._card_id = int(item.card_id)
        self._kind = "undo"
        self._show_answer = False
        self._render_current()
        self._set_status("undone")

    def action_preview(self) -> None:
        if self._card_id is None:
            return
        if getattr(self._backend, "name", "") != "direct" or not hasattr(self._backend, "_store"):
            self._set_status("preview is supported only for direct backend")
            return

        store = cast(Any, self._backend._store)
        try:
            items = store.preview_ratings(int(self._card_id))
        except Exception as exc:
            self._set_status(f"preview failed: {exc}")
            return

        lines: list[str] = []
        for it in items:
            if not isinstance(it, Mapping):
                continue
            ease = it.get("ease")
            due_info = it.get("due_info")
            ivl = it.get("interval")
            queue = it.get("queue")
            lines.append(f"ease={ease} queue={queue} interval={ivl} due_info={due_info}")

        body = "\n".join(lines) if lines else "(no preview data)"
        self.push_screen(PreviewScreen("Preview ratings", body))

    def action_focus_command(self) -> None:
        cmd = self.query_one("#cmd", Input)
        cmd.focus()
        if not cmd.value:
            cmd.value = ":"
    
    def action_blur_command(self) -> None:
        cmd = self.query_one("#cmd", Input)
        cmd.value = ""
        self.set_focus(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.value = ""

        if not raw:
            return

        line = raw[1:].strip() if raw.startswith(":") else raw
        self._run_command(line)

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _load_next(self) -> None:
        try:
            # Prefer the direct store picker when present.
            if getattr(self._backend, "name", "") == "direct" and hasattr(self._backend, "_store"):
                store = cast(Any, self._backend._store)
                if hasattr(store, "get_next_due_card"):
                    picked = store.get_next_due_card(self._deck)
                    cid = picked.get("card_id") if isinstance(picked, dict) else None
                    kind = str(picked.get("kind", "none")) if isinstance(picked, dict) else "none"
                    self._card_id = int(cid) if isinstance(cid, int) else None
                    self._kind = kind
                else:
                    self._card_id, self._kind = pick_next_due_card_id(
                        self._backend, deck=self._deck
                    )
            else:
                self._card_id, self._kind = pick_next_due_card_id(self._backend, deck=self._deck)
        except Exception as exc:
            self._card_id = None
            self._kind = "error"
            self._set_status(f"failed to pick next card: {exc}")
            self.query_one("#card", Static).update("")
            return

        self._show_answer = False
        if self._card_id is None:
            deck = self._deck or "(all)"
            self._set_status(f"No due cards. deck={deck}")
            self.query_one("#card", Static).update("")
            return

        self._render_current()
        self._set_status(f"card_id={self._card_id} kind={self._kind}")

    def _render_current(self) -> None:
        assert self._card_id is not None
        try:
            rendered = self._render_card(self._card_id, reveal_answer=self._show_answer)
        except Exception as exc:
            self.query_one("#card", Static).update(f"render failed: {exc}")
            return

        question = _strip_html_basic(str(rendered.get("question") or ""))
        answer = _strip_html_basic(str(rendered.get("answer") or ""))

        text = question if not self._show_answer else f"{question}\n\n{'-'*40}\n\n{answer}"
        self.query_one("#card", Static).update(text)

    def _render_card(self, card_id: int, *, reveal_answer: bool) -> dict[str, Any]:
        card_obj = self._backend.get_card(int(card_id))
        card_map = cast(Mapping[str, Any], card_obj) if isinstance(card_obj, Mapping) else {}

        note_id = _extract_note_id(card_map)
        ord_ = _extract_ord(card_map)
        if note_id is None:
            raise RuntimeError("card has no note id")

        fields_map = self._backend.get_note_fields(note_id=int(note_id), fields=None)

        notetype_name: str | None = None
        raw_nt = card_map.get("notetype_name")
        if isinstance(raw_nt, str) and raw_nt.strip():
            notetype_name = raw_nt.strip()
        else:
            note_obj = self._backend.get_note(int(note_id))
            if isinstance(note_obj, Mapping) and isinstance(note_obj.get("modelName"), str):
                notetype_name = str(note_obj["modelName"]).strip()

        if not notetype_name:
            raise RuntimeError("unable to determine notetype")

        nt_detail = self._backend.get_notetype(notetype_name)
        kind = str(nt_detail.get("kind", "normal")).lower()

        templates_raw = nt_detail.get("templates")
        templates_map: Mapping[str, Any]
        if isinstance(templates_raw, Mapping):
            templates_map = cast(Mapping[str, Any], templates_raw)
        else:
            templates_map = {}
        tpl = _pick_template(templates_map, ord_)
        if tpl is None:
            raise RuntimeError(f"no templates found for notetype {notetype_name}")

        front_tmpl = str(tpl.get("Front") or "")
        back_tmpl = str(tpl.get("Back") or "")

        if kind == "cloze":
            cloze_index = ord_ + 1
            question = render_template(
                front_tmpl,
                fields_map,
                cloze_index=cloze_index,
                reveal_cloze=False,
            )
            answer = render_template(
                back_tmpl,
                fields_map,
                front_side=question,
                cloze_index=cloze_index,
                reveal_cloze=True,
            )
        else:
            question = render_template(front_tmpl, fields_map)
            answer = render_template(back_tmpl, fields_map, front_side=question)

        return {"question": question, "answer": answer if reveal_answer else ""}

    def _run_command(self, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self._set_status(f"parse error: {exc}")
            return

        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in {"help", "h", "?"}:
            self.push_screen(
                PreviewScreen(
                    "Commands",
                    "\n".join(
                        [
                            ":next                pick next due card",
                            ":show                show answer",
                            ":hide                hide answer",
                            ":rate 1|2|3|4         answer (again|hard|good|easy)",
                            ":again|hard|good|easy answer",
                            ":undo                undo last answer (direct only)",
                            ":preview              preview rating outcomes (direct only)",
                            ":deck <name>          set deck filter (use quotes for ::)",
                            ":quit                 quit",
                        ]
                    ),
                )
            )
            return

        if cmd in {"quit", "exit", "q"}:
            self.exit()
            return

        if cmd == "deck":
            self._deck = " ".join(args).strip() or None
            self._load_next()
            return

        if cmd == "next":
            self._load_next()
            return

        if cmd == "show":
            if self._card_id is None:
                return
            self._show_answer = True
            self._render_current()
            return

        if cmd == "hide":
            if self._card_id is None:
                return
            self._show_answer = False
            self._render_current()
            return

        if cmd in {"again", "hard", "good", "easy"}:
            mapping = {"again": 1, "hard": 2, "good": 3, "easy": 4}
            self.action_rate(mapping[cmd])
            return

        if cmd in {"rate", "answer"}:
            if not args:
                self._set_status("usage: :rate 1|2|3|4")
                return
            try:
                ease = int(args[0])
            except ValueError:
                self._set_status("usage: :rate 1|2|3|4")
                return
            if ease not in {1, 2, 3, 4}:
                self._set_status("usage: :rate 1|2|3|4")
                return
            self.action_rate(ease)
            return

        if cmd == "undo":
            self.action_undo()
            return

        if cmd == "preview":
            self.action_preview()
            return

        self._set_status(f"unknown command: {cmd} (try :help)")