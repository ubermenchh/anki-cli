from __future__ import annotations

import contextlib
import html as _html
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static, Tab, Tabs
from textual.worker import get_current_worker

from anki_cli import __version__

from ._utils import due_day_label, relative_eta, to_int
from .theming import DEFAULT_PALETTE, Palette, ThemedApp

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

# Queue label -> Palette role, so the colours follow the active theme.
_QUEUE_ROLES: dict[str, str] = {
    "New": "primary",
    "Learn": "warning",
    "Review": "success",
    "Suspended": "error",
    "Buried": "muted",
}

# Question first: it is what the user scans, and it is the one column that
# absorbs whatever width is left. The short columns are fixed so Due and Ivl
# are always on screen (#79: they used to sit past the right edge at any width).
# Cards fetched per backend call while loading; also the progress granularity.
_LOAD_BATCH = 200

_BROWSE_COLUMNS = ("Question", "Deck", "Due", "Ivl", "Type")
_FIXED_COLUMN_WIDTHS: dict[str, int] = {"Deck": 18, "Due": 11, "Ivl": 5, "Type": 8}
_MIN_QUESTION_WIDTH = 12
_CELL_PADDING = 2  # DataTable pads each cell by one column on each side
_SCROLLBAR_WIDTH = 1


def _question_width(table_width: int) -> int:
    """Width left for the Question column once the fixed columns, cell padding
    and the vertical scrollbar are taken out of ``table_width``."""
    fixed = sum(_FIXED_COLUMN_WIDTHS.values())
    padding = _CELL_PADDING * len(_BROWSE_COLUMNS)
    return max(_MIN_QUESTION_WIDTH, table_width - fixed - padding - _SCROLLBAR_WIDTH)


def _deck_leaf(deck: str) -> str:
    """``Languages::Japanese::Core 2k`` -> ``Core 2k``; the preview has the full path."""
    return deck.rsplit("::", 1)[-1] if deck else ""


def _queue_label(queue: Any) -> str:
    if queue is None:
        return ""
    return QUEUE_LABELS.get(to_int(queue), str(queue))


def _queue_color(queue: Any, pal: Palette = DEFAULT_PALETTE) -> str:
    return str(getattr(pal, _QUEUE_ROLES.get(_queue_label(queue), "muted")))


def _strip_html_basic(value: str) -> str:
    """Strip HTML for table cells — <br> becomes space instead of newline."""
    text = _BR_RE.sub(" ", value)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


def _truncate(text: str, length: int = 80) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1] + "\u2026"


def _format_card_detail(card: Mapping[str, Any], pal: Palette = DEFAULT_PALETTE) -> Text:
    """Format full card details for the detail modal."""
    t = Text()

    def _row(label: str, value: str, value_style: str = pal.text) -> None:
        t.append(f"{label:<12}", style=pal.muted)
        t.append(str(value), style=value_style)
        t.append("\n")

    _row("Card ID:", str(card.get("cardId", "")))
    _row("Note ID:", str(card.get("note", "")))
    _row("Deck:", str(card.get("deckName", "")), pal.accent)
    _row("Notetype:", str(card.get("notetype_name", "")))
    _row("Ord:", str(card.get("ord", "")))
    _row("Type:", str(card.get("type", "")))

    queue_int = card.get("queue")
    queue_label = QUEUE_LABELS.get(int(queue_int), str(queue_int)) if queue_int is not None else ""
    queue_color = str(getattr(pal, _QUEUE_ROLES.get(queue_label, "muted")))
    _row("Queue:", f"{queue_label} ({queue_int})", queue_color)

    _row("Due:", str(card.get("due_info", "")))
    _row("Interval:", str(card.get("interval", "")))
    _row("Factor:", str(card.get("factor", "")))
    _row("Reps:", str(card.get("reps", "")))

    lapses_val = card.get("lapses", "")
    lapses_int = int(lapses_val) if isinstance(lapses_val, int) else 0
    _row("Lapses:", str(lapses_val), pal.error if lapses_int > 3 else pal.text)
    _row("Flags:", str(card.get("flags", "")))

    values = _extract_field_values(card)
    if values:
        t.append("\n")
        t.append("--- Fields ---\n", style=f"bold {pal.warning}")
        for i, value in enumerate(values):
            text = _strip_html_basic(value)
            t.append(f"  [{i}] ", style=pal.muted)
            t.append(f"{text}\n", style=pal.text)

    tags = card.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        t.append("\n")
        t.append("Tags: ", style=pal.muted)
        t.append(", ".join(str(tag) for tag in tags), style=pal.accent)
        t.append("\n")

    return t


def _format_due_short(card: Mapping[str, Any]) -> str:
    queue = to_int(card.get("queue"), 0)
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
            epoch = to_int(due_info.get("epoch_secs"), 0)
            return relative_eta(epoch)
        if kind in ("review_day_index", "learn_day_index"):
            return due_day_label(due_info)
        if kind == "new_position":
            return "new"

    if due_info in ("", None):
        fallback = _queue_label(queue)
        return fallback.lower() if fallback else ""
    return _truncate(_strip_html_basic(str(due_info)), 16)


def _format_interval_short(card: Mapping[str, Any]) -> str:
    ivl = to_int(card.get("interval"), 0)
    if ivl <= 0:
        return "-"
    return f"{ivl}d"


def _extract_field_values(card: Mapping[str, Any]) -> list[str]:
    """Field values in notetype order. Both backends emit ``fields`` as a list
    (#30); the raw AnkiConnect mapping is still accepted for hand-built dicts."""
    fields = card.get("fields")
    if isinstance(fields, (list, tuple)):
        return [str(v) for v in fields]
    if isinstance(fields, Mapping):
        from anki_cli.backends.normalize import ordered_fields

        return ordered_fields(fields)[1]
    return []


def _extract_front_back(card: Mapping[str, Any]) -> tuple[str, str]:
    values = _extract_field_values(card)
    front = _strip_html_basic(values[0]) if len(values) > 0 else ""
    back = _strip_html_basic(values[1]) if len(values) > 1 else ""
    return front, back


def _extract_note_id_from_card(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _format_browser_row(
    card: Mapping[str, Any], *, question_width: int = 56, pal: Palette = DEFAULT_PALETTE
) -> tuple[Text | str, ...]:
    """One table row in ``_BROWSE_COLUMNS`` order: Question, Deck, Due, Ivl, Type."""
    front, _ = _extract_front_back(card)
    question_text = " ".join(front.split())
    question = Text(_truncate(question_text, question_width), style=pal.text)

    deck_name = str(card.get("deckName", ""))
    deck = Text(_truncate(_deck_leaf(deck_name), _FIXED_COLUMN_WIDTHS["Deck"]), style=pal.accent)

    due_label = _format_due_short(card)
    qcolor = _queue_color(card.get("queue"), pal)
    due = Text()
    due.append("● ", style=qcolor)
    due.append(_truncate(due_label, _FIXED_COLUMN_WIDTHS["Due"] - 2), style=qcolor)

    ivl = Text(_format_interval_short(card), style=pal.muted)

    notetype = _truncate(str(card.get("notetype_name") or "-"), _FIXED_COLUMN_WIDTHS["Type"])
    if "cloze" in notetype.lower():
        type_style = pal.secondary
    elif notetype == "-":
        type_style = pal.muted
    else:
        type_style = pal.success
    notetype_text = Text(notetype, style=type_style)

    return (question, deck, due, ivl, notetype_text)


class CardTable(DataTable[Text | str]):
    """DataTable that tells the app when its width changed, so the Question
    column can be re-fitted (#79). ``Resize`` itself does not bubble."""

    class Resized(Message):
        pass

    # The table owns Enter (RowSelected opens the detail view), so the legend
    # entry lives here, where Footer looks for the focused widget's bindings.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "select_cursor", "Detail", key_display="Enter", show=True),
    ]

    def on_resize(self, event: events.Resize) -> None:
        self.post_message(self.Resized())


class DetailScreen(ModalScreen[None]):
    """Modal screen showing full card details."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "app.pop_screen", "Close")]

    DEFAULT_CSS = """
        DetailScreen {
            align: center middle;
            background: $background 70%;
        }
        DetailScreen #detail-panel {
            width: 80%;
            max-width: 100;
            height: 80%;
            background: $surface;
            border: round $border;
            padding: 1 2;
        }
        DetailScreen #detail-title {
            height: auto;
            color: $accent;
            text-style: bold;
            padding-bottom: 1;
        }
        DetailScreen #detail-body {
            height: 1fr;
        }
        DetailScreen #detail-body Static {
            color: $foreground;
        }
        DetailScreen #detail-hint {
            height: auto;
            color: $text-muted;
            padding-top: 1;
            text-align: right;
        }
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


class BrowseApp(ThemedApp):
    """Interactive card browser with split table + preview pane."""

    TITLE = "anki-cli browse"

    # Focus is placed on the table explicitly in on_mount; Textual's default
    # AUTO_FOCUS="*" landed on the search Input, so q/d/s/r/1-5 typed into the
    # query instead of acting (#81).
    AUTO_FOCUS = None

    # Wide terminals put the preview beside the table; narrow ones stack it
    # under the table as a short strip. CSS keys off the class Textual puts on
    # the Screen, so there is no resize handler for the layout (#84).
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (100, "-wide")]

    CSS = """
    Screen {
        background: $background;
    }
    /* One docked container per edge: docking two widgets to the same edge
       makes them overlap (#78), so the bars live inside #top in normal flow. */
    /* Footer colours are set here, not left to the theme's footer-* variables,
       which assume a footer on a $primary background. */
    Footer {
        background: $surface;
    }
    Footer FooterKey, Footer .footer-key--key, Footer .footer-key--description {
        background: transparent;
    }
    Footer .footer-key--key {
        color: $primary;
    }
    Footer .footer-key--description {
        color: $text-muted;
    }
    /* --no-color: "auto" contrast colours do not resolve against the terminal's
       own background, so use its plain foreground. */
    .-no-color Footer .footer-key--description {
        color: $foreground;
    }
    #top {
        dock: top;
        height: auto;
    }
    #header {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    #header-text {
        width: 1fr;
        color: $primary;
        text-style: bold;
    }
    #header-count {
        width: auto;
        color: $text-muted;
    }
    #search-wrap {
        height: 1;
        background: $background;
        padding: 0 1;
    }
    #search-prefix {
        width: 2;
        color: $accent;
        text-style: bold;
    }
    #search {
        height: 1;
        padding: 0;
        border: none;
        background: $background;
        color: $foreground;
    }
    #search:focus {
        border: none;
        background: $surface;
    }
    #filters {
        height: 2;
        background: $background;
    }
    #main {
        height: 1fr;
        layout: horizontal;
    }
    #table {
        width: 3fr;
        height: 1fr;
        background: $background;
        border: round $border;
        border-title-color: $text-muted;
        scrollbar-size: 1 1;
        scrollbar-color: $border;
        scrollbar-color-hover: $text-muted;
        scrollbar-background: $background;
    }
    #table:focus {
        background-tint: transparent;
        border: round $primary;
        border-title-color: $primary;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $panel;
        color: $foreground;
    }
    DataTable > .datatable--even-row {
        background: $background;
    }
    DataTable > .datatable--odd-row {
        background: $surface;
    }
    #preview {
        width: 2fr;
        min-width: 30;
        max-width: 60;
        height: 1fr;
        background: $background;
        border: round $border;
        border-title-color: $text-muted;
        padding: 0 1;
    }
    #preview:focus-within {
        border: round $primary;
        border-title-color: $primary;
    }
    #preview.-hidden {
        display: none;
    }
    /* Fixed columns + a 12-col Question + border need 68; the preview takes the rest. */
    Screen.-wide #table {
        min-width: 68;
    }
    /* Narrow: the preview is a strip under the table, not a column. */
    Screen.-narrow #main {
        layout: vertical;
    }
    Screen.-narrow #table {
        width: 1fr;
    }
    Screen.-narrow #preview {
        width: 1fr;
        max-width: 100%;
        height: 8;
    }
    #preview-scroll {
        height: 1fr;
    }
    #preview-body {
        height: auto;
        color: $foreground;
    }
    #preview-meta {
        height: auto;
        color: $text-muted;
        padding-top: 1;
    }
    """

    # Footer renders the ``show=True`` entries for the focused widget, so this
    # list is also the on-screen key legend. ``?`` opens the full list (#7).
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "focus_search", "Filter", key_display="/", show=True),
        Binding("s", "suspend_selected", "Suspend", show=True),
        Binding("d", "delete_selected", "Delete", show=True),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("left_square_bracket", "prev_filter", "Prev tab", key_display="[", show=False),
        Binding("right_square_bracket", "next_filter", "Next tab", key_display="]", show=False),
        Binding("1", "focus_pane('table')", "Cards", show=False),
        Binding("2", "focus_pane('preview-scroll')", "Preview", show=False),
        Binding("p", "toggle_preview", "Preview on/off", show=False),
        Binding("f", "toggle_zoom", "Zoom pane", show=False),
        Binding("question_mark", "show_help_panel", "Help", key_display="?", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "escape", "Back / Quit", show=False),
    ]

    _FILTER_ORDER: ClassVar[tuple[str, ...]] = ("all", "new", "review", "learn", "suspended")
    _FILTER_LABELS: ClassVar[dict[str, str]] = {
        "all": "All",
        "new": "New",
        "review": "Review",
        "learn": "Learn",
        "suspended": "Suspended",
    }

    def __init__(
        self,
        *,
        backend: Any,
        query: str = "",
        theme: str | None = None,
        no_color: bool = False,
        persist_theme: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(theme=theme, no_color=no_color, persist_theme=persist_theme)
        self._header_state: tuple[str, str] = ("", "")
        self._backend = backend
        self._query = query
        self._cards: list[dict[str, Any]] = []
        self._visible_cards: list[dict[str, Any]] = []
        self._active_filter = "all"
        self._last_cursor_row = -999
        self._delete_arm_note_id: int | None = None
        self._current_question_width = 56

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            with Horizontal(id="header"):
                yield Static(f"anki-cli {__version__} · browse", id="header-text")
                yield Static("", id="header-count")
            with Horizontal(id="search-wrap"):
                yield Static("⌕ ", id="search-prefix")
                yield Input(placeholder="search query...", id="search")
            yield Tabs(
                *(Tab(self._FILTER_LABELS[key], id=f"ftab-{key}") for key in self._FILTER_ORDER),
                id="filters",
            )
        with Horizontal(id="main"):
            yield CardTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="preview"), VerticalScroll(id="preview-scroll"):
                yield Static("", id="preview-body")
                yield Static("", id="preview-meta")
        yield Footer(compact=True)

    def on_palette_changed(self) -> None:
        """Rich text bakes colours in, so rebuild it for the new theme."""
        self._render_header(*self._header_state)
        if self._visible_cards:
            self._populate_table(reset_cursor=False)
        self._sync_preview_cursor(force=True)

    def on_mount(self) -> None:
        self._theme_ready()
        self._add_columns()
        self.query_one("#table").border_title = "1 Cards"
        self.query_one("#preview").border_title = "2 Preview"

        search = self.query_one("#search", Input)
        search.value = self._query

        self._render_header(self._query or "*", "")
        self._render_preview_empty()

        self.query_one("#table", DataTable).focus()
        self._load_cards(self._query)

    @on(CardTable.Resized)
    def _refit_columns(self) -> None:
        # Column widths are fixed at add time, so a resize re-fits Question
        # to the table's new width by rebuilding the columns (#79).
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            if table.size.width and self._question_width() != self._current_question_width:
                row = to_int(getattr(table, "cursor_row", -1), -1)
                self._populate_table()
                if 0 <= row < table.row_count:
                    table.move_cursor(row=row, column=0)

    def _question_width(self) -> int:
        with contextlib.suppress(Exception):
            width = self.query_one("#table", DataTable).size.width
            if width > 0:
                return _question_width(width)
        return 56

    def _add_columns(self) -> None:
        table = self.query_one("#table", DataTable)
        self._current_question_width = self._question_width()
        for name in _BROWSE_COLUMNS:
            width = _FIXED_COLUMN_WIDTHS.get(name, self._current_question_width)
            table.add_column(name, key=name, width=width)

    def action_escape(self) -> None:
        # Esc from the search box goes back to the table; it used to quit the
        # whole app (#81). Esc from anywhere else still quits.
        if isinstance(self.focused, Input):
            self.query_one("#table", DataTable).focus()
            return
        self.exit()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self._load_cards(self._current_query())

    def action_prev_filter(self) -> None:
        self._step_filter(-1)

    def action_next_filter(self) -> None:
        self._step_filter(1)

    def _step_filter(self, delta: int) -> None:
        idx = self._FILTER_ORDER.index(self._active_filter)
        self._set_filter(self._FILTER_ORDER[(idx + delta) % len(self._FILTER_ORDER)])

    def action_focus_pane(self, widget_id: str) -> None:
        preview = self.query_one("#preview")
        if widget_id == "preview-scroll" and not preview.display:
            return
        self.query_one(f"#{widget_id}").focus()

    def action_toggle_preview(self) -> None:
        preview = self.query_one("#preview")
        preview.toggle_class("-hidden")
        if preview.has_class("-hidden"):
            self.query_one("#table").focus()

    def action_toggle_zoom(self) -> None:
        screen = self.screen
        if screen.maximized is not None:
            screen.minimize()
        elif self.focused is not None and not isinstance(self.focused, Input):
            screen.maximize(self.focused)

    def action_show_detail(self) -> None:
        row_idx = self._selected_row_index()
        if row_idx >= 0:
            self._show_detail_for_row(row_idx)

    def action_suspend_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            self._toast("No card selected", severity="warning")
            return

        card_id = to_int(card.get("cardId"), 0)
        if card_id <= 0:
            self._toast("Selected row has no card id", severity="error")
            return

        self._suspend_card(card_id, to_int(card.get("queue"), 0))

    @work(thread=True, group="mutate")
    def _suspend_card(self, card_id: int, queue: int) -> None:
        # Off the UI thread: the backend call opens/locks the collection.
        try:
            if queue == -1:
                self._backend.unsuspend_cards([card_id])
                action = "unsuspended"
            else:
                self._backend.suspend_cards([card_id])
                action = "suspended"
        except Exception as exc:
            self._toast(f"Suspend failed: {exc}", severity="error")
            return

        self._toast(f"Card {card_id} {action}")
        self._load_cards(self._current_query())

    def action_delete_selected(self) -> None:
        card = self._selected_card()
        if card is None:
            self._toast("No card selected", severity="warning")
            return

        note_id = _extract_note_id_from_card(card)
        if note_id is None:
            self._toast("Selected card has no note id", severity="error")
            return

        if self._delete_arm_note_id != note_id:
            self._delete_arm_note_id = note_id
            self._toast(f"Press d again to delete note {note_id}", severity="warning")
            return

        self._delete_arm_note_id = None
        self._delete_note(note_id)

    @work(thread=True, group="mutate")
    def _delete_note(self, note_id: int) -> None:
        try:
            result = self._backend.delete_notes([note_id])
        except Exception as exc:
            self._toast(f"Delete failed: {exc}", severity="error")
            return

        deleted_notes = 0
        if isinstance(result, Mapping):
            deleted_notes = to_int(result.get("deleted_notes"), 0)
            if deleted_notes == 0:
                deleted_notes = to_int(result.get("deleted"), 0)

        if deleted_notes > 0:
            self._toast(f"Deleted note {note_id}")
        else:
            self._toast(f"Note {note_id} was not deleted", severity="warning")

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

    @on(Tabs.TabActivated, "#filters")
    def _on_filter_tab(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        if tab_id.startswith("ftab-"):
            self._set_filter(tab_id[5:])

    def _current_query(self) -> str:
        with contextlib.suppress(Exception):
            return self.query_one("#search", Input).value.strip()
        return self._query

    def _selected_row_index(self) -> int:
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            return to_int(getattr(table, "cursor_row", -1), -1)
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
        detail = _format_card_detail(card, self.palette)
        self.push_screen(DetailScreen(f"Card {card.get('cardId', '')}", detail))

    def _set_filter(self, filter_key: str) -> None:
        if filter_key not in self._FILTER_ORDER or filter_key == self._active_filter:
            return
        self._active_filter = filter_key
        self._delete_arm_note_id = None
        self._visible_cards = [card for card in self._cards if self._matches_filter(card)]
        self._populate_table(reset_cursor=True)
        self._render_filter_tabs()
        self._sync_preview_cursor(force=True)
        self._set_status(self._current_query() or "*", self._count_label())

    def _matches_filter(self, card: Mapping[str, Any], filter_key: str | None = None) -> bool:
        key = filter_key or self._active_filter
        queue = to_int(card.get("queue"), 0)

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

    def _count_label(self) -> str:
        return f"{len(self._visible_cards)}/{len(self._cards)} cards"

    def _render_filter_tabs(self) -> None:
        # Keep the Tabs strip in step when the filter changed from the keyboard.
        with contextlib.suppress(Exception):
            tabs = self.query_one("#filters", Tabs)
            if tabs.active != f"ftab-{self._active_filter}":
                tabs.active = f"ftab-{self._active_filter}"

    def _render_header(self, left: str, right: str) -> None:
        # Text(): ``left`` is often the raw user query, which must not be
        # parsed as markup (#82).
        self._header_state = (left, right)
        pal = self.palette
        title = Text(f"anki-cli {__version__} · browse · ")
        title.append(left, style=f"bold {pal.text}")
        with contextlib.suppress(Exception):
            self.query_one("#header-text", Static).update(title)
        with contextlib.suppress(Exception):
            self.query_one("#header-count", Static).update(Text(right, style=pal.muted))

    def _toast(self, message: str, *, severity: str = "information") -> None:
        """Transient result of an action; replaces the old persistent status line."""
        if threading.current_thread() is threading.main_thread():
            self.notify(Text(message).plain, severity=severity)  # type: ignore[arg-type]
        else:
            with contextlib.suppress(Exception):
                self.call_from_thread(self.notify, message, severity=severity)

    @on(DataTable.RowHighlighted, "#table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._sync_preview_cursor()

    def _sync_preview_cursor(self, force: bool = False) -> None:
        with contextlib.suppress(Exception):
            table = self.query_one("#table", DataTable)
            row = to_int(getattr(table, "cursor_row", -1), -1)
            if force or row != self._last_cursor_row:
                self._last_cursor_row = row
                self._delete_arm_note_id = None
                self._update_preview_for_row(row)

    def _render_preview_empty(self) -> None:
        self.query_one("#preview").border_title = "2 Preview"
        self.query_one("#preview-body", Static).update("No card selected.")
        self.query_one("#preview-meta", Static).update("")

    def _update_preview_for_row(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._visible_cards)):
            self._render_preview_empty()
            return

        card = self._visible_cards[row_idx]
        front, back = _extract_front_back(card)

        card_id = str(card.get("cardId", ""))
        queue = card.get("queue")
        queue_label = _queue_label(queue)
        pal = self.palette
        queue_color = _queue_color(queue, pal)

        self.query_one("#preview").border_title = f"2 Preview · card {card_id}"

        body = Text()
        body.append("FRONT\n", style=f"bold {pal.muted}")
        body.append(front or "(empty)", style=f"bold {pal.text}")
        body.append("\n\n")
        body.append("─" * 14 + " answer " + "─" * 14, style=pal.muted)
        body.append("\n\n")
        body.append(back or "(empty)", style=pal.text)
        self.query_one("#preview-body", Static).update(body)

        reps = to_int(card.get("reps"), 0)
        lapses = to_int(card.get("lapses"), 0)
        factor = to_int(card.get("factor"), 0)
        ivl_text = _format_interval_short(card)

        tags_raw = card.get("tags")
        if isinstance(tags_raw, (list, tuple)):
            tags = ", ".join(str(t) for t in tags_raw) or "-"
        else:
            tags = "-"

        meta = Text()
        meta.append(" Queue    ", style=pal.muted)
        meta.append(f"{queue_label or '-':<10}", style=f"bold {queue_color}")
        meta.append(" Interval ", style=pal.muted)
        meta.append(f"{ivl_text}\n", style=f"bold {pal.primary}")
        meta.append(" Reps     ", style=pal.muted)
        meta.append(f"{reps!s:<10}", style=pal.text)
        meta.append(" Lapses   ", style=pal.muted)
        meta.append(f"{lapses!s}\n", style=pal.error if lapses > 3 else pal.text)
        meta.append(" Factor   ", style=pal.muted)
        meta.append(f"{factor!s:<10}", style=f"bold {pal.warning}")
        meta.append(" Tags     ", style=pal.muted)
        meta.append(tags, style=pal.accent)
        self.query_one("#preview-meta", Static).update(meta)

    def _set_status(self, left: str, right: str) -> None:
        """Header state (query + count, or loading progress)."""
        if threading.current_thread() is threading.main_thread():
            self._render_header(left, right)
        else:
            with contextlib.suppress(Exception):
                self.call_from_thread(self._render_header, left, right)

    def _on_cards_loaded(self, cards: list[dict[str, Any]], query: str) -> None:
        self._cards = cards
        self._visible_cards = [card for card in self._cards if self._matches_filter(card)]
        self._populate_table(reset_cursor=True)
        self._render_filter_tabs()
        self._sync_preview_cursor(force=True)
        self._set_status(query or "*", self._count_label())
        if self.focused is None:
            with contextlib.suppress(Exception):
                self.query_one("#table", DataTable).focus()

    def _populate_table(self, *, reset_cursor: bool = False) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        self._add_columns()
        pal = self.palette
        for card in self._visible_cards:
            table.add_row(
                *_format_browser_row(card, question_width=self._current_question_width, pal=pal)
            )

        if self._visible_cards:
            current_row = to_int(getattr(table, "cursor_row", -1), -1)
            if reset_cursor or current_row < 0 or current_row >= len(self._visible_cards):
                with contextlib.suppress(Exception):
                    table.move_cursor(row=0, column=0)
        else:
            self._last_cursor_row = -999

    @work(thread=True, exclusive=True, group="load")
    def _load_cards(self, query: str) -> None:
        self._set_status("loading...", "")
        try:
            card_ids = self._backend.find_cards(query=query)
            total = len(card_ids)
            cards: list[dict[str, Any]] = []

            for lo in range(0, total, _LOAD_BATCH):
                if get_current_worker().is_cancelled:
                    return
                cards.extend(
                    card
                    for card in self._backend.get_cards(card_ids[lo : lo + _LOAD_BATCH])
                    if isinstance(card, dict)
                )
                if lo + _LOAD_BATCH < total:
                    self._set_status(f"loading {len(cards)}/{total}...", "")

            if get_current_worker().is_cancelled:
                return
            self.call_from_thread(self._on_cards_loaded, cards, query)
        except Exception as exc:
            self._toast(f"Load failed: {exc}", severity="error")
            self._set_status("error", "0 cards")
