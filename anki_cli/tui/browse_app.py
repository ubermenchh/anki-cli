from __future__ import annotations

import contextlib
import html as _html
import re
import threading
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from anki_cli import __version__

from .colors import (
    BLUE,
    BORDER,
    CYAN,
    DIM,
    GREEN,
    MENU_BG,
    RED,
    SELECTION_BG,
    TEXT,
    TOOLBAR_BG,
    YELLOW,
)

PURPLE = "#a29bfe"
ORANGE = "#ff9f43"

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")

QUEUE_LABELS: dict[int, str] = {
    0: "New",
    1: "Learn",
    2: "Review",
    3: "Learn",
    -1: "Suspended",
    -2: "Buried",
    -3: "Buried",
}

_QUEUE_COLORS: dict[str, str] = {
    "New": BLUE,
    "Learn": YELLOW,
    "Review": GREEN,
    "Suspended": RED,
    "Buried": DIM,
}

_COLUMNS = ("ID", "Deck", "Type", "Question", "Due", "Queue", "Interval", "Reps", "Lapses")

_BROWSE_COLUMNS = ("Deck", "Type", "Question", "Due", "Ivl")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _queue_label(queue: Any) -> str:
    if queue is None:
        return ""
    return QUEUE_LABELS.get(_to_int(queue), str(queue))


def _queue_color(queue: Any) -> str:
    return _QUEUE_COLORS.get(_queue_label(queue), DIM)


def _strip_html_basic(value: str) -> str:
    """Strip HTML for table cells — <br> becomes space instead of newline."""
    text = _BR_RE.sub(" ", value)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _truncate(text: str, length: int = 80) -> str:
    if len(text) <= length:
        return text
    return text[:length - 1] + "\u2026"


def _format_card_row(card: Mapping[str, Any]) -> tuple[Text | str, ...]:
    card_id = Text(str(card.get("cardId", "")), style=DIM)
    deck = Text(str(card.get("deckName", "")), style=CYAN)
    notetype = Text(str(card.get("notetype_name", "")), style=DIM)

    fields = card.get("fields")
    if isinstance(fields, (list, tuple)) and fields:
        question = Text(_truncate(_strip_html_basic(str(fields[0]))), style=TEXT)
    else:
        question = Text("", style=TEXT)

    due = Text(str(card.get("due_info", "")), style=DIM)

    queue_int = card.get("queue")
    queue_label = QUEUE_LABELS.get(int(queue_int), str(queue_int)) if queue_int is not None else ""
    queue_color = _QUEUE_COLORS.get(queue_label, DIM)
    queue = Text(queue_label, style=queue_color)

    interval = Text(str(card.get("interval", "")), style=DIM)
    reps = Text(str(card.get("reps", "")), style=DIM)
    lapses_val = card.get("lapses", "")
    lapses_int = int(lapses_val) if isinstance(lapses_val, int) else 0
    lapses = Text(str(lapses_val), style=RED if lapses_int > 3 else DIM)

    return (card_id, deck, notetype, question, due, queue, interval, reps, lapses)


def _format_card_detail(card: Mapping[str, Any]) -> Text:
    """Format full card details for the detail modal."""
    t = Text()

    def _row(label: str, value: str, value_style: str = TEXT) -> None:
        t.append(f"{label:<12}", style=DIM)
        t.append(str(value), style=value_style)
        t.append("\n")

    _row("Card ID:", str(card.get("cardId", "")))
    _row("Note ID:", str(card.get("note", "")))
    _row("Deck:", str(card.get("deckName", "")), CYAN)
    _row("Notetype:", str(card.get("notetype_name", "")))
    _row("Ord:", str(card.get("ord", "")))
    _row("Type:", str(card.get("type", "")))

    queue_int = card.get("queue")
    queue_label = QUEUE_LABELS.get(int(queue_int), str(queue_int)) if queue_int is not None else ""
    queue_color = _QUEUE_COLORS.get(queue_label, DIM)
    _row("Queue:", f"{queue_label} ({queue_int})", queue_color)

    _row("Due:", str(card.get("due_info", "")))
    _row("Interval:", str(card.get("interval", "")))
    _row("Factor:", str(card.get("factor", "")))
    _row("Reps:", str(card.get("reps", "")))

    lapses_val = card.get("lapses", "")
    lapses_int = int(lapses_val) if isinstance(lapses_val, int) else 0
    _row("Lapses:", str(lapses_val), RED if lapses_int > 3 else TEXT)
    _row("Flags:", str(card.get("flags", "")))

    fields = card.get("fields")
    if isinstance(fields, (list, tuple)):
        t.append("\n")
        t.append("--- Fields ---\n", style=f"bold {YELLOW}")
        for i, f in enumerate(fields):
            text = _strip_html_basic(str(f))
            t.append(f"  [{i}] ", style=DIM)
            t.append(f"{text}\n", style=TEXT)

    tags = card.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        t.append("\n")
        t.append("Tags: ", style=DIM)
        t.append(", ".join(str(tag) for tag in tags), style=CYAN)
        t.append("\n")

    return t

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


def _format_due_short(card: Mapping[str, Any]) -> str:
    queue = _to_int(card.get("queue"), 0)
    due_info = card.get("due_info")

    if queue == 0:
        return "new"
    if queue == -1:
        return "suspended"
    if queue in (-2, -3):
        return "buried"

    if isinstance(due_info, Mapping):
        kind = str(due_info.get("kind") or "")
        if kind == "learn_epoch_secs":
            epoch = _to_int(due_info.get("epoch_secs"), 0)
            return _relative_eta(epoch)
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
            return f"d{day_index}" if day_index is not None else "review"
        if kind == "new_position":
            return "new"

    if due_info in ("", None):
        fallback = _queue_label(queue)
        return fallback.lower() if fallback else ""
    return _truncate(_strip_html_basic(str(due_info)), 16)


def _format_interval_short(card: Mapping[str, Any]) -> str:
    ivl = _to_int(card.get("interval"), 0)
    if ivl <= 0:
        return "-"
    return f"{ivl}d"


def _extract_front_back(card: Mapping[str, Any]) -> tuple[str, str]:
    fields = card.get("fields")
    if isinstance(fields, (list, tuple)):
        front = _strip_html_basic(str(fields[0])) if len(fields) > 0 else ""
        back = _strip_html_basic(str(fields[1])) if len(fields) > 1 else ""
        return front, back
    return "", ""


def _extract_note_id_from_card(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _format_browser_row(card: Mapping[str, Any]) -> tuple[Text | str, ...]:
    deck = Text(_truncate(str(card.get("deckName", "")), 16), style=CYAN)

    notetype = _truncate(str(card.get("notetype_name", "") or "-"), 10)
    notetype_lower = notetype.lower()
    if "cloze" in notetype_lower:
        type_style = PURPLE
    elif notetype == "-":
        type_style = DIM
    else:
        type_style = GREEN
    notetype_text = Text(notetype, style=type_style)

    front, _ = _extract_front_back(card)
    question_text = " ".join(front.split())
    question = Text(_truncate(question_text, 56), style=TEXT)

    due_label = _format_due_short(card)
    qcolor = _queue_color(card.get("queue"))
    due = Text()
    due.append("● ", style=qcolor)
    due.append(due_label, style=qcolor)

    ivl = Text(_format_interval_short(card), style=DIM)

    return (deck, notetype_text, question, due, ivl)


class DetailScreen(ModalScreen[None]):
    """Modal screen showing full card details."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "app.pop_screen", "Close")]

    DEFAULT_CSS = f"""
        DetailScreen {{
            align: center middle;
            background: rgba(0, 0, 0, 0.65);
        }}
        DetailScreen #detail-panel {{
            width: 80%;
            max-width: 100;
            height: 80%;
            background: {MENU_BG};
            border: round {BORDER};
            padding: 1 2;
        }}
        DetailScreen #detail-title {{
            height: auto;
            color: {CYAN};
            text-style: bold;
            padding-bottom: 1;
        }}
        DetailScreen #detail-body {{
            height: 1fr;
        }}
        DetailScreen #detail-body Static {{
            color: {TEXT};
        }}
        DetailScreen #detail-hint {{
            height: auto;
            color: {DIM};
            padding-top: 1;
            text-align: right;
        }}
    """

    def __init__(self, title: str, body: str | Text) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-panel"):
            yield Static(self._title, id="detail-title")
            with VerticalScroll(id="detail-body"):
                yield Static(self._body)
            yield Static("esc to close", id="detail-hint")


class BrowseApp(App[None]):
    """Interactive card browser with split table + preview pane."""

    TITLE = "anki-cli browse"

    CSS = f"""
    Screen {{
        background: {TOOLBAR_BG};
    }}

    #titlebar {{
        height: 2;
        dock: top;
        background: {MENU_BG};
        border-bottom: solid {BORDER};
        padding: 0 1;
    }}
    #titlebar-text {{
        width: auto;
        color: {BLUE};
        text-style: bold;
    }}
    #titlebar-keys {{
        width: 1fr;
        text-align: right;
        color: {DIM};
    }}

    #toolbar {{
        height: 3;
        dock: top;
        background: {TOOLBAR_BG};
        border-bottom: solid {BORDER};
        padding: 0 1;
    }}
    #search-wrap {{
        width: 1fr;
        height: 3;
        padding-right: 1;
    }}
    #search-prefix {{
        width: 2;
        content-align: left middle;
        color: {CYAN};
        text-style: bold;
    }}
    #search {{
        height: 3;
        border: none;
        background: {TOOLBAR_BG};
        color: {TEXT};
    }}
    #search:focus {{
        border: none;
        background: {MENU_BG};
    }}
    #filter-tabs {{
        width: auto;
        height: 3;
        content-align: left middle;
        padding-right: 1;
    }}
    .filter-chip {{
        min-width: 0;
        width: auto;
        height: 1;
        border: none;
        background: transparent;
        color: {DIM};
        padding: 0 1 0 1;
        margin: 0 1 0 0;
    }}
    .filter-chip:hover {{
        background: {SELECTION_BG};
        color: {TEXT};
    }}
    .filter-chip.-active {{
        background: {SELECTION_BG};
        color: {BLUE};
        text-style: bold;
    }}
    #toolbar-count {{
        width: 24;
        text-align: right;
        color: {DIM};
        content-align: right middle;
    }}

    #main {{
        height: 1fr;
    }}

    DataTable {{
        width: 1fr;
        height: 1fr;
        background: {TOOLBAR_BG};
        scrollbar-size: 1 1;
        scrollbar-color: {BORDER};
        scrollbar-color-hover: {DIM};
        scrollbar-background: {TOOLBAR_BG};
    }}
    DataTable > .datatable--header {{
        background: {MENU_BG};
        color: {BLUE};
        text-style: bold;
    }}
    DataTable > .datatable--cursor {{
        background: {SELECTION_BG};
        color: {TEXT};
    }}
    DataTable > .datatable--even-row {{
        background: {TOOLBAR_BG};
    }}
    DataTable > .datatable--odd-row {{
        background: {MENU_BG};
    }}

    #preview {{
        width: 44;
        background: {MENU_BG};
        border-left: solid {BORDER};
        padding: 1;
    }}
    #preview-title {{
        height: auto;
        color: {DIM};
        text-style: bold;
        padding-bottom: 1;
    }}
    #preview-scroll {{
        height: 1fr;
    }}
    #preview-body {{
        height: auto;
        color: {TEXT};
        padding-bottom: 1;
    }}
    #preview-meta {{
        height: auto;
        color: {DIM};
        padding-top: 1;
    }}
    #preview-actions {{
        height: auto;
        color: {DIM};
        padding-top: 1;
        border-top: solid {BORDER};
    }}

    #statusbar {{
        height: 1;
        background: {MENU_BG};
        border-top: solid {BORDER};
        padding: 0 1;
    }}
    #statusbar-text {{
        width: 1fr;
        color: {DIM};
    }}
    #statusbar-count {{
        width: 24;
        text-align: right;
        color: {DIM};
    }}

    #hintbar {{
        height: auto;
        background: {TOOLBAR_BG};
        border-top: solid {BORDER};
        color: {DIM};
        padding: 0 1;
    }}
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "quit", "Quit", show=False),
        Binding("slash", "focus_search", "Search", key_display="/", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("tab", "cycle_filter", "Cycle filter", show=False),
        Binding("1", "filter_all", "All", show=False),
        Binding("2", "filter_new", "New", show=False),
        Binding("3", "filter_review", "Review", show=False),
        Binding("4", "filter_learn", "Learn", show=False),
        Binding("5", "filter_suspended", "Suspended", show=False),
        Binding("enter", "show_detail", "Detail", show=False),
        Binding("e", "edit_selected", "Edit", show=False),
        Binding("d", "delete_selected", "Delete", show=False),
        Binding("s", "suspend_selected", "Suspend", show=False),
    ]

    _FILTER_ORDER: ClassVar[tuple[str, ...]] = ("all", "new", "review", "learn", "suspended")
    _FILTER_LABELS: ClassVar[dict[str, str]] = {
        "all": "All",
        "new": "New",
        "review": "Review",
        "learn": "Learn",
        "suspended": "Suspended",
    }

    def __init__(self, *, backend: Any, query: str = "") -> None:
        super().__init__()
        self._backend = backend
        self._query = query
        self._cards: list[dict[str, Any]] = []
        self._visible_cards: list[dict[str, Any]] = []
        self._active_filter = "all"
        self._last_cursor_row = -999
        self._delete_arm_note_id: int | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="titlebar"):
            yield Static(f"anki-cli {__version__} — browse", id="titlebar-text")
            yield Static("", id="titlebar-keys")

        with Horizontal(id="toolbar"):
            with Horizontal(id="search-wrap"):
                yield Static("⌕ ", id="search-prefix")
                yield Input(placeholder="search query...", id="search")
            with Horizontal(id="filter-tabs"):
                for idx, key in enumerate(self._FILTER_ORDER, start=1):
                    label = self._FILTER_LABELS[key]
                    yield Button(
                        f"[{idx}] {label}", id=f"fchip-{key}", classes="filter-chip",
                    )
            yield Static("", id="toolbar-count")

        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)

            with Vertical(id="preview"):
                yield Static("Preview", id="preview-title")
                with VerticalScroll(id="preview-scroll"):
                    yield Static("", id="preview-body")
                    yield Static("", id="preview-meta")
                yield Static("", id="preview-actions")

        with Horizontal(id="statusbar"):
            yield Static("", id="statusbar-text")
            yield Static("", id="statusbar-count")

        yield Static("", id="hintbar")

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column(_BROWSE_COLUMNS[0], key=_BROWSE_COLUMNS[0], width=16)
        table.add_column(_BROWSE_COLUMNS[1], key=_BROWSE_COLUMNS[1], width=10)
        table.add_column(_BROWSE_COLUMNS[2], key=_BROWSE_COLUMNS[2], width=56)
        table.add_column(_BROWSE_COLUMNS[3], key=_BROWSE_COLUMNS[3], width=10)
        table.add_column(_BROWSE_COLUMNS[4], key=_BROWSE_COLUMNS[4], width=6)

        search = self.query_one("#search", Input)
        search.value = self._query

        self._render_filter_tabs()
        self._render_toolbar_count()
        self._render_hint_bar()
        self._render_preview_empty()

        self.set_interval(0.12, self._sync_preview_cursor)
        self._load_cards(self._query)

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self._load_cards(self._current_query())

    def action_cycle_filter(self) -> None:
        current = self._active_filter
        idx = self._FILTER_ORDER.index(current)
        next_filter = self._FILTER_ORDER[(idx + 1) % len(self._FILTER_ORDER)]
        self._set_filter(next_filter)

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_new(self) -> None:
        self._set_filter("new")

    def action_filter_review(self) -> None:
        self._set_filter("review")

    def action_filter_learn(self) -> None:
        self._set_filter("learn")

    def action_filter_suspended(self) -> None:
        self._set_filter("suspended")

    def action_show_detail(self) -> None:
        row_idx = self._selected_row_index()
        if row_idx >= 0:
            self._show_detail_for_row(row_idx)

    def action_edit_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            self._set_status("no card selected", self._count_label())
            return
        self._set_status("edit UI not implemented yet - showing detail", self._count_label())
        self.action_show_detail()

    def action_suspend_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            self._set_status("no card selected", self._count_label())
            return

        card_id = _to_int(card.get("cardId"), 0)
        if card_id <= 0:
            self._set_status("selected row has no card id", self._count_label())
            return

        queue = _to_int(card.get("queue"), 0)
        try:
            if queue == -1:
                self._backend.unsuspend_cards([card_id])
                action = "unsuspended"
            else:
                self._backend.suspend_cards([card_id])
                action = "suspended"
        except Exception as exc:
            self._set_status(f"suspend failed: {exc}", self._count_label())
            return

        self._set_status(f"{action} card {card_id}", self._count_label())
        self._load_cards(self._current_query())

    def action_delete_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            self._set_status("no card selected", self._count_label())
            return

        note_id = _extract_note_id_from_card(card)
        if note_id is None:
            self._set_status("selected card has no note id", self._count_label())
            return

        if self._delete_arm_note_id != note_id:
            self._delete_arm_note_id = note_id
            self._set_status(
                f"press d again to delete note {note_id}",
                self._count_label(),
            )
            return

        self._delete_arm_note_id = None
        try:
            result = self._backend.delete_notes([note_id])
        except Exception as exc:
            self._set_status(f"delete failed: {exc}", self._count_label())
            return

        deleted_notes = 0
        if isinstance(result, Mapping):
            deleted_notes = _to_int(result.get("deleted_notes"), 0)
            if deleted_notes == 0:
                deleted_notes = _to_int(result.get("deleted"), 0)

        if deleted_notes > 0:
            self._set_status(f"deleted note {note_id}", self._count_label())
        else:
            self._set_status(f"note {note_id} not deleted", self._count_label())

        self._load_cards(self._current_query())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        self._load_cards(event.value.strip())
        self.query_one("#table", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_idx = self._row_index_from_key(event.row_key)
        if row_idx is not None:
            self._show_detail_for_row(row_idx)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("fchip-"):
            filter_key = btn_id[6:]
            if filter_key in self._FILTER_ORDER:
                self._set_filter(filter_key)

    def _current_query(self) -> str:
        with contextlib.suppress(Exception):
            return self.query_one("#search", Input).value.strip()
        return self._query

    def _selected_row_index(self) -> int:
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            return _to_int(getattr(table, "cursor_row", -1), -1)
        return -1

    def _selected_card(self) -> dict[str, Any] | None:
        row = self._selected_row_index()
        if 0 <= row < len(self._visible_cards):
            return self._visible_cards[row]
        return None

    def _row_index_from_key(self, row_key: Any) -> int | None:
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            return list(table.rows.keys()).index(row_key)
        return None

    def _show_detail_for_row(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._visible_cards)):
            return
        card = self._visible_cards[row_idx]
        detail = _format_card_detail(card)
        self.push_screen(DetailScreen(f"Card {card.get('cardId', '')}", detail))

    def _set_filter(self, filter_key: str) -> None:
        if filter_key not in self._FILTER_ORDER:
            return
        self._active_filter = filter_key
        self._delete_arm_note_id = None
        self._visible_cards = [card for card in self._cards if self._matches_filter(card)]
        self._populate_table(reset_cursor=True)
        self._render_filter_tabs()
        self._render_toolbar_count()
        self._sync_preview_cursor(force=True)
        self._set_status(self._current_query() or "*", self._count_label())

    def _matches_filter(self, card: Mapping[str, Any], filter_key: str | None = None) -> bool:
        key = filter_key or self._active_filter
        queue = _to_int(card.get("queue"), 0)

        if key == "all":
            return True
        if key == "new":
            return queue == 0
        if key == "review":
            return queue == 2
        if key == "learn":
            return queue in (1, 3)
        if key == "suspended":
            return queue == -1
        return True

    def _count_for_filter(self, filter_key: str) -> int:
        return sum(1 for card in self._cards if self._matches_filter(card, filter_key))

    def _count_label(self) -> str:
        return f"{len(self._visible_cards)}/{len(self._cards)} cards"

    def _render_filter_tabs(self) -> None:
        for key in self._FILTER_ORDER:
            with contextlib.suppress(Exception):
                btn = self.query_one(f"#fchip-{key}", Button)
                if key == self._active_filter:
                    btn.add_class("-active")
                else:
                    btn.remove_class("-active")

    def _render_toolbar_count(self) -> None:
        count = Text()
        count.append(str(len(self._visible_cards)), style=TEXT)
        count.append(" cards", style=DIM)
        self.query_one("#toolbar-count", Static).update(count)

    def _render_hint_bar(self) -> None:
        hint = Text()
        shortcuts = [
            ("/", "search"), ("↑↓", "navigate"), ("e", "edit"),
            ("a", "add"), ("Tab", "filter"),
        ]
        for i, (key, label) in enumerate(shortcuts):
            if i > 0:
                hint.append("   ")
            hint.append(f" {key} ", style=f"bold {BLUE}")
            hint.append(f" {label}", style=DIM)
        hint.append("   ")
        hint.append(" q ", style=f"bold {BLUE}")
        hint.append(" back", style=DIM)
        self.query_one("#hintbar", Static).update(hint)

    def _sync_preview_cursor(self, force: bool = False) -> None:
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            row = _to_int(getattr(table, "cursor_row", -1), -1)
            if force or row != self._last_cursor_row:
                self._last_cursor_row = row
                self._delete_arm_note_id = None
                self._update_preview_for_row(row)

    def _render_preview_empty(self) -> None:
        self.query_one("#preview-title", Static).update("Preview")
        self.query_one("#preview-body", Static).update("No card selected.")
        self.query_one("#preview-meta", Static).update("")
        actions = Text()
        for key, label in [("e", "Edit"), ("d", "Delete"), ("s", "Suspend"), ("Enter", "Study")]:
            actions.append(f" {key} ", style=f"bold {BLUE}")
            actions.append(f" {label}  ", style=DIM)
        self.query_one("#preview-actions", Static).update(actions)

    def _update_preview_for_row(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._visible_cards)):
            self._render_preview_empty()
            return

        card = self._visible_cards[row_idx]
        front, back = _extract_front_back(card)

        card_id = str(card.get("cardId", ""))
        queue = card.get("queue")
        queue_label = _queue_label(queue)
        queue_color = _queue_color(queue)

        title = Text()
        title.append("PREVIEW — CARD ", style=DIM)
        title.append(card_id, style=CYAN)
        self.query_one("#preview-title", Static).update(title)

        body = Text()
        body.append("FRONT\n", style=f"bold {DIM}")
        body.append(front or "(empty)", style=f"bold {TEXT}")
        body.append("\n\n")
        body.append("─" * 14 + " answer " + "─" * 14, style=DIM)
        body.append("\n\n")
        body.append(back or "(empty)", style=TEXT)
        self.query_one("#preview-body", Static).update(body)

        reps = _to_int(card.get("reps"), 0)
        lapses = _to_int(card.get("lapses"), 0)
        factor = _to_int(card.get("factor"), 0)
        ivl_text = _format_interval_short(card)

        tags_raw = card.get("tags")
        if isinstance(tags_raw, (list, tuple)):
            tags = ", ".join(str(t) for t in tags_raw) or "-"
        else:
            tags = "-"

        meta = Text()
        meta.append(" Queue    ", style=DIM)
        meta.append(f"{queue_label or '-':<10}", style=f"bold {queue_color}")
        meta.append(" Interval ", style=DIM)
        meta.append(f"{ivl_text}\n", style=f"bold {BLUE}")
        meta.append(" Reps     ", style=DIM)
        meta.append(f"{reps!s:<10}", style=TEXT)
        meta.append(" Lapses   ", style=DIM)
        meta.append(f"{lapses!s}\n", style=RED if lapses > 3 else TEXT)
        meta.append(" Factor   ", style=DIM)
        meta.append(f"{factor!s:<10}", style=f"bold {ORANGE}")
        meta.append(" Tags     ", style=DIM)
        meta.append(tags, style=CYAN)
        self.query_one("#preview-meta", Static).update(meta)

        suspend_label = "Unsuspend" if _to_int(card.get("queue"), 0) == -1 else "Suspend"
        actions = Text()
        action_items = [("e", "Edit"), ("d", "Delete"), ("s", suspend_label), ("Enter", "Study")]
        for key, label in action_items:
            actions.append(f" {key} ", style=f"bold {BLUE}")
            actions.append(f" {label}  ", style=DIM)
        self.query_one("#preview-actions", Static).update(actions)

    def _set_status(self, left: str, right: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._update_status_widget(left, right)
        else:
            with contextlib.suppress(Exception):
                self.call_from_thread(self._update_status_widget, left, right)

    def _update_status_widget(self, left: str, right: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#statusbar-text", Static).update(left)
        with contextlib.suppress(Exception):
            self.query_one("#statusbar-count", Static).update(right)

    def _on_cards_loaded(self, cards: list[dict[str, Any]], query: str) -> None:
        self._cards = cards
        self._visible_cards = [card for card in self._cards if self._matches_filter(card)]
        self._populate_table(reset_cursor=True)
        self._render_filter_tabs()
        self._render_toolbar_count()
        self._sync_preview_cursor(force=True)
        self._set_status(query or "*", self._count_label())

    def _populate_table(self, *, reset_cursor: bool = False) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        for card in self._visible_cards:
            table.add_row(*_format_browser_row(card))

        if self._visible_cards:
            current_row = _to_int(getattr(table, "cursor_row", -1), -1)
            if reset_cursor or current_row < 0 or current_row >= len(self._visible_cards):
                with contextlib.suppress(Exception):
                    table.move_cursor(row=0, column=0)
        else:
            self._last_cursor_row = -999

    @work(thread=True)
    def _load_cards(self, query: str) -> None:
        self._set_status("loading...", "")
        try:
            card_ids = self._backend.find_cards(query=query)
            cards: list[dict[str, Any]] = []
            total = len(card_ids)

            for i, cid in enumerate(card_ids):
                try:
                    card = self._backend.get_card(cid)
                    if isinstance(card, dict):
                        cards.append(card)
                except Exception:
                    pass

                if total > 0 and (i + 1) % 50 == 0:
                    self._set_status(f"loading {i + 1}/{total}...", "")

            self.call_from_thread(self._on_cards_loaded, cards, query)
        except Exception as exc:
            self._set_status(f"error: {exc}", "0 cards")
