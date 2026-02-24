from __future__ import annotations

import contextlib
import html as _html
import re
import shlex
import time
from collections.abc import Mapping
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from anki_cli import __version__
from anki_cli.core.scheduler import pick_next_due_card_id
from anki_cli.core.template import render_template
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms
from anki_cli.tui.colors import (
    BLUE,
    BORDER,
    CYAN,
    DIM,
    GREEN,
    MENU_BG,
    RED,
    TEXT,
    TOOLBAR_BG,
    YELLOW,
)

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

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _queue_name(queue: int) -> str:
    mapping = {
        0: "New",
        1: "Learn",
        2: "Review",
        3: "Learn",
        -1: "Suspended",
        -2: "Buried",
        -3: "Buried",
    }
    return mapping.get(queue, str(queue))


def _relative_eta(epoch_secs: int) -> str:
    now = int(time.time())
    delta = max(0, int(epoch_secs) - now)
    if delta < 60:
        return "<1m"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = (hours + 23) // 24
    return f"{days}d"


def _format_due_info_short(due_info: Any) -> str:
    if isinstance(due_info, Mapping):
        kind = str(due_info.get("kind") or "")
        if kind == "new_position":
            return "new"
        if kind == "learn_epoch_secs":
            epoch = due_info.get("epoch_secs")
            if isinstance(epoch, int):
                return _relative_eta(epoch)
            return "learn"
        if kind == "review_day_index":
            epoch = due_info.get("epoch_secs")
            if isinstance(epoch, int):
                now = int(time.time())
                days = max(0, (epoch - now) // 86400)
                if days == 0:
                    return "today"
                if days == 1:
                    return "tomorrow"
                return f"{days}d"
            day_index = due_info.get("day_index")
            if isinstance(day_index, int):
                return f"d{day_index}"
            return "review"
        raw = due_info.get("raw")
        return str(raw) if raw is not None else "?"
    if due_info is None:
        return "-"
    return str(due_info)


def _progress_bar(pct: int, width: int = 20) -> str:
    clamped = max(0, min(100, pct))
    filled = round((clamped / 100.0) * width)
    return ("█" * filled) + ("░" * (width - filled))


class PreviewScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "app.pop_screen", "Close", show=False),
        Binding("q", "app.pop_screen", "Close", show=False),
    ]

    DEFAULT_CSS = f"""
    PreviewScreen {{
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }}

    PreviewScreen #preview-panel {{
        width: 84%;
        max-width: 120;
        height: 80%;
        background: {MENU_BG};
        border: round {BORDER};
        padding: 1 2;
    }}

    PreviewScreen #preview-title {{
        height: auto;
        color: {CYAN};
        text-style: bold;
        padding-bottom: 1;
        border-bottom: solid {BORDER};
    }}

    PreviewScreen #preview-body {{
        height: 1fr;
        margin-top: 1;
        background: {TOOLBAR_BG};
        border: round {BORDER};
        padding: 1;
    }}

    PreviewScreen #preview-content {{
        color: {TEXT};
    }}

    PreviewScreen #preview-hint {{
        height: auto;
        color: {DIM};
        text-align: right;
        padding-top: 1;
    }}
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-panel"):
            yield Static(self._title, id="preview-title")
            with VerticalScroll(id="preview-body"):
                yield Static(self._body, id="preview-content")
            yield Static("esc/q to close", id="preview-hint")


class ReviewApp(App[None]):
    CSS = f"""
    Screen {{
        background: {TOOLBAR_BG};
    }}

    #topbar {{
        dock: top;
        height: 2;
        background: {MENU_BG};
        border-bottom: solid {BORDER};
        padding: 0 1;
    }}
    #topbar-title {{
        width: auto;
        color: {BLUE};
        text-style: bold;
    }}
    #topbar-right {{
        width: 1fr;
        text-align: right;
        color: {DIM};
    }}

    #hintbar {{
        dock: bottom;
        height: 1;
        background: {MENU_BG};
        border-top: solid {BORDER};
        color: {DIM};
        padding: 0 1;
    }}
    #status {{
        dock: bottom;
        height: 1;
        background: {TOOLBAR_BG};
        color: {DIM};
        padding: 0 1;
    }}
    #cmd {{
        dock: bottom;
        height: 1;
        border: none;
        background: {MENU_BG};
        color: {TEXT};
        padding: 0 1;
    }}
    #cmd:focus {{
        background: {TOOLBAR_BG};
    }}

    #body {{
        height: 1fr;
    }}
    #study-main {{
        width: 1fr;
        border-right: solid {BORDER};
        padding: 1 2;
    }}
    #study-side {{
        width: 30;
        background: {MENU_BG};
        padding: 1;
    }}

    #counter {{
        height: 1;
        text-align: right;
        color: {DIM};
    }}
    #deck-badge {{
        height: 1;
        color: {BLUE};
        text-style: bold;
        text-align: center;
    }}

    .panel-label {{
        height: 1;
        color: {DIM};
        text-style: bold;
    }}

    #question-panel {{
        height: 1fr;
        min-height: 6;
        border: round {BORDER};
        background: {MENU_BG};
        padding: 1 2;
    }}
    #question {{
        color: {TEXT};
    }}

    #answer-panel {{
        height: 1fr;
        min-height: 6;
        border: round {BLUE};
        background: {TOOLBAR_BG};
        padding: 1 2;
    }}
    #answer {{
        color: {TEXT};
    }}

    #rate-row {{
        height: 3;
    }}
    .rate-btn {{
        width: 1fr;
        height: 3;
        border: round {BORDER};
        content-align: center middle;
        text-align: center;
    }}
    #rate-1 {{
        color: {RED};
        border: round {RED};
    }}
    #rate-2 {{
        color: {YELLOW};
        border: round {YELLOW};
    }}
    #rate-3 {{
        color: {BLUE};
        border: round {BLUE};
    }}
    #rate-4 {{
        color: {GREEN};
        border: round {GREEN};
    }}

    .side-title {{
        height: 1;
        color: {DIM};
        text-style: bold;
        border-bottom: solid {BORDER};
    }}
    .side-block {{
        height: auto;
        color: {TEXT};
        padding-bottom: 1;
    }}
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

        self._current_card: dict[str, Any] | None = None
        self._session_total = 0
        self._answered = 0
        self._rating_counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(f"anki-cli {__version__} — study", id="topbar-title")
            yield Static("", id="topbar-right")

        with Horizontal(id="body"):
            with Vertical(id="study-main"):
                yield Static("", id="counter")
                yield Static("", id="deck-badge")

                yield Static("QUESTION", classes="panel-label")
                with VerticalScroll(id="question-panel"):
                    yield Static("", id="question")

                yield Static("ANSWER", classes="panel-label")
                with VerticalScroll(id="answer-panel"):
                    yield Static("", id="answer")

                with Horizontal(id="rate-row"):
                    yield Static("", id="rate-1", classes="rate-btn")
                    yield Static("", id="rate-2", classes="rate-btn")
                    yield Static("", id="rate-3", classes="rate-btn")
                    yield Static("", id="rate-4", classes="rate-btn")

            with Vertical(id="study-side"):
                yield Static("SESSION", classes="side-title")
                yield Static("", id="session-block", classes="side-block")

                yield Static("TODAY", classes="side-title")
                yield Static("", id="today-block", classes="side-block")

                yield Static("REMAINING", classes="side-title")
                yield Static("", id="remaining-block", classes="side-block")

                yield Static("SHORTCUTS", classes="side-title")
                yield Static("", id="shortcuts-block", classes="side-block")

        yield Static("", id="hintbar")
        yield Static("", id="status")
        yield Input(placeholder=":help", id="cmd")

    def on_mount(self) -> None:
        self._render_hint_bar()
        self._render_shortcuts()
        self._refresh_rate_buttons()
        self._refresh_chrome()
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

        self._answered += 1
        self._rating_counts[int(ease)] = self._rating_counts.get(int(ease), 0) + 1
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

        if self._answered > 0:
            self._answered -= 1

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
        self.push_screen(PreviewScreen(f"Preview ratings - card {self._card_id}", body))

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

    def _ui_update(self, selector: str, value: str | Text) -> None:
        with contextlib.suppress(Exception):
            self.query_one(selector, Static).update(value)

    def _set_status(self, msg: str) -> None:
        self._ui_update("#status", msg)

    def _safe_due_counts(self) -> dict[str, int]:
        try:
            raw = self._backend.get_due_counts(deck=self._deck)
        except Exception:
            return {"new": 0, "learn": 0, "review": 0, "total": 0}

        if not isinstance(raw, Mapping):
            return {"new": 0, "learn": 0, "review": 0, "total": 0}

        new_count = _safe_int(raw.get("new"), 0)
        learn_count = _safe_int(raw.get("learn"), 0)
        review_count = _safe_int(raw.get("review"), 0)
        total_count = _safe_int(raw.get("total"), new_count + learn_count + review_count)

        return {
            "new": new_count,
            "learn": learn_count,
            "review": review_count,
            "total": total_count,
        }

    def _rating_hints(self) -> dict[int, str]:
        hints = {1: "<10m", 2: "-", 3: "-", 4: "-"}
        if self._card_id is None:
            return hints

        if getattr(self._backend, "name", "") != "direct" or not hasattr(self._backend, "_store"):
            return hints

        store = cast(Any, self._backend._store)
        try:
            items = store.preview_ratings(int(self._card_id))
        except Exception:
            return hints

        for item in items:
            if not isinstance(item, Mapping):
                continue
            ease = _safe_int(item.get("ease"), 0)
            if ease not in hints:
                continue
            hints[ease] = _format_due_info_short(item.get("due_info"))

        return hints

    def _refresh_rate_buttons(self) -> None:
        hints = self._rating_hints()
        self._ui_update("#rate-1", f"1 Again\\n{hints[1]}")
        self._ui_update("#rate-2", f"2 Hard\\n{hints[2]}")
        self._ui_update("#rate-3", f"3 Good\\n{hints[3]}")
        self._ui_update("#rate-4", f"4 Easy\\n{hints[4]}")

    def _render_hint_bar(self) -> None:
        hint = Text()
        shortcuts = [
            ("Space", "show answer"), ("1-4", "rate card"), ("e", "edit"),
            ("m", "mark"), ("s", "suspend"),
        ]
        for i, (key, label) in enumerate(shortcuts):
            if i > 0:
                hint.append("   ")
            hint.append(f" {key} ", style=f"bold {BLUE}")
            hint.append(f" {label}", style=DIM)
        hint.append("   ")
        hint.append(" q ", style=f"bold {BLUE}")
        hint.append(" quit session", style=DIM)
        self._ui_update("#hintbar", hint)

    def _render_shortcuts(self) -> None:
        sc = Text()
        for key, label in [("Space", "flip"), ("e", "edit"), ("q", "quit")]:
            sc.append(f" {key} ", style=f"bold {BLUE}")
            sc.append(f"  {label}\n", style=DIM)
        self._ui_update("#shortcuts-block", sc)

    def _refresh_chrome(self) -> None:
        counts = self._safe_due_counts()
        remaining_total = counts["total"]

        if self._session_total <= 0:
            self._session_total = remaining_total

        done_from_remaining = max(0, self._session_total - remaining_total)
        done = max(done_from_remaining, self._answered)

        if self._session_total > 0:
            pct = int((done * 100) / self._session_total)
        else:
            pct = 100 if remaining_total == 0 else 0
        pct = max(0, min(100, pct))

        deck_name = self._deck or "all decks"
        self._ui_update("#topbar-right", f"{deck_name} · {remaining_total} remaining")
        self._ui_update("#counter", f"{done} / {self._session_total} · {pct}%")

        bar = _progress_bar(pct, width=20)
        session = Text()
        session.append(f" {bar} ", style=BLUE)
        session.append(f" {pct}%\n", style=f"bold {BLUE}")
        session.append(f" {done}/{self._session_total} done", style=DIM)
        self._ui_update("#session-block", session)

        today = Text()
        today.append("Again  ", style=DIM)
        today.append(f"{self._rating_counts.get(1, 0)}\n", style=f"bold {RED}")
        today.append("Hard   ", style=DIM)
        today.append(f"{self._rating_counts.get(2, 0)}\n", style=f"bold {YELLOW}")
        today.append("Good   ", style=DIM)
        today.append(f"{self._rating_counts.get(3, 0)}\n", style=f"bold {BLUE}")
        today.append("Easy   ", style=DIM)
        today.append(f"{self._rating_counts.get(4, 0)}", style=f"bold {GREEN}")
        self._ui_update("#today-block", today)

        remaining = Text()
        remaining.append("New     ", style=BLUE)
        remaining.append(f"{counts['new']}\n", style=f"bold {BLUE}")
        remaining.append("Learn   ", style=YELLOW)
        remaining.append(f"{counts['learn']}\n", style=f"bold {YELLOW}")
        remaining.append("Review  ", style=GREEN)
        remaining.append(f"{counts['review']}", style=f"bold {GREEN}")
        self._ui_update("#remaining-block", remaining)

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
                    self._card_id, self._kind = (
                        pick_next_due_card_id(self._backend, deck=self._deck)
                    )
            else:
                self._card_id, self._kind = pick_next_due_card_id(self._backend, deck=self._deck)
        except Exception as exc:
            self._card_id = None
            self._kind = "error"
            self._current_card = None
            self._set_status(f"failed to pick next card: {exc}")
            self._ui_update("#question", "")
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            return

        self._show_answer = False

        if self._card_id is None:
            deck = self._deck or "(all)"
            self._current_card = None
            self._set_status(f"No due cards. deck={deck}")
            self._ui_update("#deck-badge", deck)
            self._ui_update("#question", "No due cards.")
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            return

        self._current_card = None
        with contextlib.suppress(Exception):
            card_obj = self._backend.get_card(int(self._card_id))
            if isinstance(card_obj, Mapping):
                self._current_card = dict(card_obj)

        self._render_current()
        self._set_status(f"card_id={self._card_id} kind={self._kind}")

    def _render_current(self) -> None:
        assert self._card_id is not None
        try:
            rendered = self._render_card(self._card_id, reveal_answer=self._show_answer)
        except Exception as exc:
            self._ui_update("#question", f"render failed: {exc}")
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            return

        question = _strip_html_basic(str(rendered.get("question") or ""))
        answer = _strip_html_basic(str(rendered.get("answer") or ""))

        self._ui_update("#question", question or "(empty question)")
        if self._show_answer:
            self._ui_update("#answer", answer or "(empty answer)")
        else:
            self._ui_update("#answer", "[space] show answer")

        deck_badge = self._deck
        if not deck_badge and isinstance(self._current_card, Mapping):
            raw_deck = self._current_card.get("deckName")
            if isinstance(raw_deck, str) and raw_deck.strip():
                deck_badge = raw_deck.strip()
        badge = Text()
        badge.append(f"  {(deck_badge or 'all decks').upper()}  ", style=f"bold {BLUE}")
        self._ui_update("#deck-badge", badge)

        self._refresh_rate_buttons()
        self._refresh_chrome()

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
            self._session_total = 0
            self._answered = 0
            self._rating_counts = {1: 0, 2: 0, 3: 0, 4: 0}
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