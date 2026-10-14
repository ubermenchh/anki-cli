from __future__ import annotations

import contextlib
import html as _html
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from anki_cli import __version__

from ._utils import due_day_label, relative_eta, to_int
from .prompts import PromptScreen, TokenSuggester
from .review_app import ReviewScreen
from .theming import DEFAULT_PALETTE, PALETTE_CSS, Palette, ThemedApp, command

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


def _format_due_long(card: Mapping[str, Any]) -> str:
    """Human due line for the detail modal, e.g. ``2026-10-20 · in 5 days``."""
    queue = to_int(card.get("queue"), 0)
    due_info = card.get("due_info")

    if queue == -1:
        return "suspended"
    if queue in (-2, -3):
        return "buried"
    if not isinstance(due_info, Mapping):
        return "-" if due_info in ("", None) else str(due_info)

    kind = str(due_info.get("kind") or "")
    if kind == "new_position":
        raw = due_info.get("raw")
        return f"new card #{raw}" if raw is not None else "new"

    epoch = to_int(due_info.get("epoch_secs"), 0)
    if kind == "learn_epoch_secs":
        when = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else ""
        return f"{when} · {relative_eta(epoch)}" if when else relative_eta(epoch)

    if kind in ("review_day_index", "learn_day_index"):
        days = due_info.get("days_from_today")
        if isinstance(days, int):
            if days == 0:
                rel = "today"
            elif days == 1:
                rel = "tomorrow"
            elif days == -1:
                rel = "yesterday"
            elif days > 1:
                rel = f"in {days} days"
            else:
                rel = f"{-days} days overdue"
        else:
            rel = due_day_label(due_info)
        date = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") if epoch else ""
        return f"{date} · {rel}" if date else rel

    raw = due_info.get("raw")
    return str(raw) if raw is not None else "-"


def _format_card_detail(card: Mapping[str, Any], pal: Palette = DEFAULT_PALETTE) -> Group:
    """The detail modal body: a summary, the fields by name, tags, then the ids."""

    def grid(label_width: int) -> Table:
        t = Table.grid(padding=(0, 2))
        t.add_column(width=label_width, no_wrap=True)
        t.add_column(ratio=1, overflow="fold")
        return t

    def label(text: str) -> Text:
        return Text(text, style=pal.muted)

    def value(text: Any, style: str | None = None) -> Text:
        return Text(str(text), style=style or pal.text)

    queue_int = card.get("queue")
    queue_label = _queue_label(queue_int)
    interval = to_int(card.get("interval"), 0)
    factor = to_int(card.get("factor"), 0)
    lapses = to_int(card.get("lapses"), 0)
    flag = _flag_of(card)

    summary = grid(10)
    summary.add_row(label("Deck"), value(card.get("deckName", ""), pal.accent))
    if card.get("notetype_name"):
        summary.add_row(label("Notetype"), value(card.get("notetype_name")))
    summary.add_row(label("Queue"), value(queue_label or "-", _queue_color(queue_int, pal)))
    summary.add_row(label("Due"), value(_format_due_long(card)))
    summary.add_row(label("Interval"), value(f"{interval}d" if interval > 0 else "-"))
    summary.add_row(label("Ease"), value(f"{factor // 10}%" if factor > 0 else "-"))
    summary.add_row(label("Reps"), value(card.get("reps", 0)))
    summary.add_row(label("Lapses"), value(lapses, pal.error if lapses > 3 else pal.text))
    if flag:
        summary.add_row(label("Flag"), value(_FLAGS[flag][0], _flag_color(flag, pal)))

    parts: list[Any] = [summary]

    values = _extract_field_values(card)
    if values:
        names = card.get("field_names")
        fields = grid(10)
        for i, raw in enumerate(values):
            name = str(names[i]) if isinstance(names, (list, tuple)) and i < len(names) else ""
            fields.add_row(label(name or f"[{i}]"), value(_strip_html_basic(raw) or "(empty)"))
        parts += [Text(""), Text("FIELDS", style=f"bold {pal.warning}"), fields]

    tags = card.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        tag_grid = grid(10)
        tag_grid.add_row(label("Tags"), value(", ".join(str(tag) for tag in tags), pal.accent))
        parts += [Text(""), tag_grid]

    ids = Text(style=pal.muted)
    ids.append(f"card {card.get('cardId', '')}  ·  note {card.get('note', '')}")
    parts += [Text(""), ids]
    return Group(*parts)


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


_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::(.*?))?\}\}", re.S)


def _cloze_question(text: str) -> str:
    """``{{c1::answer::hint}}`` -> ``[hint]`` / ``[...]``, as Anki's browser shows it."""
    return _CLOZE_RE.sub(lambda m: f"[{m.group(2)}]" if m.group(2) else "[...]", text)


def _cloze_answer(text: str) -> str:
    return _CLOZE_RE.sub(lambda m: m.group(1), text)


def _extract_front_back(card: Mapping[str, Any]) -> tuple[str, str]:
    """Question and answer text. Cloze notes keep every cloze in the first
    field, so the question hides them and the answer shows them (+ Extra)."""
    values = _extract_field_values(card)
    first = _strip_html_basic(values[0]) if len(values) > 0 else ""
    second = _strip_html_basic(values[1]) if len(values) > 1 else ""
    if _CLOZE_RE.search(first):
        answer = _cloze_answer(first)
        return _cloze_question(first), f"{answer}\n\n{second}" if second else answer
    return first, second


# Anki's flag numbers: name and the Palette role it is drawn with.
_FLAGS: dict[int, tuple[str, str | None]] = {
    0: ("No flag", None),
    1: ("Red", "error"),
    2: ("Orange", "warning"),
    3: ("Green", "success"),
    4: ("Blue", "primary"),
    5: ("Pink", "secondary"),
    6: ("Turquoise", "accent"),
    7: ("Purple", "secondary"),
}


def _flag_of(card: Mapping[str, Any]) -> int:
    """The colour flag (the low three bits of ``cards.flags``)."""
    return to_int(card.get("flags"), 0) & 7


def _flag_color(flag: int, pal: Palette) -> str:
    role = _FLAGS.get(flag, ("", None))[1]
    return str(getattr(pal, role)) if role else pal.muted


def _extract_note_id_from_card(card: Mapping[str, Any]) -> int | None:
    for key in ("note", "nid", "noteId", "note_id"):
        value = card.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _format_browser_row(
    card: Mapping[str, Any],
    *,
    question_width: int = 56,
    pal: Palette = DEFAULT_PALETTE,
    marked: bool = False,
) -> tuple[Text | str, ...]:
    """One table row in ``_BROWSE_COLUMNS`` order: Question, Deck, Due, Ivl, Type."""
    front, _ = _extract_front_back(card)
    question_text = " ".join(front.split())
    # Marks and flags sit in front of the text, two cells each.
    flag = _flag_of(card)
    question = Text()
    if marked:
        question.append("\u2713 ", style=f"bold {pal.primary}")
    if flag:
        question.append("\u2691 ", style=_flag_color(flag, pal))
    prefix = 2 * (bool(marked) + bool(flag))
    question.append(_truncate(question_text, question_width - prefix), style=pal.text)

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
        # vi motions (#8): j/k move a row, g/G jump to the first/last, ctrl+d/u page.
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first_row", "First row", show=False),
        Binding("G", "last_row", "Last row", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
    ]

    def action_first_row(self) -> None:
        if self.row_count:
            self.move_cursor(row=0)

    def action_last_row(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1)

    def on_resize(self, event: events.Resize) -> None:
        self.post_message(self.Resized())


class PreviewScroll(VerticalScroll):
    """The preview pane's scroller, with vi's j/k (#8)."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
    ]


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no question for something that cannot be undone. Defaults to No."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n", "answer(False)", "No", show=False),
        Binding("escape", "answer(False)", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
        ConfirmScreen {
            align: center middle;
            background: $background 70%;
        }
        ConfirmScreen #confirm-panel {
            width: 60;
            max-width: 90%;
            height: auto;
            background: $background;
            border: round $error;
            border-title-color: $error;
            border-title-style: bold;
            padding: 1 2;
        }
        ConfirmScreen #confirm-buttons {
            height: 3;
            margin-top: 1;
        }
        ConfirmScreen Button {
            width: 1fr;
            height: 3;
            margin: 0 1 0 0;
            text-style: bold;
            tint: transparent;
        }
        /* By id: Textual's own Button.-style-default fill outranks a type selector. */
        ConfirmScreen #confirm-yes, ConfirmScreen #confirm-no {
            background: $background;
        }
        ConfirmScreen #confirm-yes {
            border: round $error;
            color: $error;
        }
        ConfirmScreen #confirm-no {
            border: round $border;
            color: $foreground;
            margin-right: 0;
        }
        ConfirmScreen #confirm-yes:hover, ConfirmScreen #confirm-yes:focus,
        ConfirmScreen #confirm-no:hover, ConfirmScreen #confirm-no:focus {
            color: $foreground;
            text-style: bold underline;
            background: $background;
            background-tint: transparent;
        }
    """

    def __init__(self, title: str, message: str, confirm_label: str = "Delete") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-panel"):
            yield Static(Text(self._message))
            with Horizontal(id="confirm-buttons"):
                yield Button(f"{self._confirm_label}  (y)", id="confirm-yes")
                yield Button("Cancel  (n)", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-panel").border_title = self._title
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class FlagScreen(ModalScreen[int | None]):
    """Pick a colour flag: 0-7, or Esc to leave it alone."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_none", "Cancel", show=False),
        *(Binding(str(n), f"pick({n})", _FLAGS[n][0], show=False) for n in range(8)),
    ]

    DEFAULT_CSS = """
        FlagScreen {
            align: center middle;
            background: $background 70%;
        }
        FlagScreen #flag-panel {
            width: 36;
            height: auto;
            background: $background;
            border: round $primary;
            border-title-color: $primary;
            border-title-style: bold;
            border-subtitle-color: $text-muted;
            padding: 1 2;
        }
        FlagScreen OptionList {
            height: auto;
            background: $background;
            border: none;
            padding: 0;
        }
        FlagScreen OptionList > .option-list--option-highlighted {
            background: $panel;
            color: $foreground;
            text-style: bold;
        }
    """

    def __init__(self, current: int, pal: Palette) -> None:
        super().__init__()
        self._current = current
        self._pal = pal

    def compose(self) -> ComposeResult:
        with Vertical(id="flag-panel"):
            yield OptionList(*(self._option(n) for n in range(8)), id="flag-list")

    def _option(self, n: int) -> Option:
        name = _FLAGS[n][0]
        line = Text()
        line.append("\u2691 " if n else "  ", style=_flag_color(n, self._pal))
        line.append(f"{n}  {name}")
        if n == self._current:
            line.append("  \u2713", style=self._pal.muted)
        return Option(line, id=str(n))

    def on_mount(self) -> None:
        panel = self.query_one("#flag-panel")
        panel.border_title = "Set flag"
        panel.border_subtitle = "0-7 or Enter \u00b7 esc cancels"
        self.query_one("#flag-list", OptionList).highlighted = self._current

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id or 0))

    def action_pick(self, n: int) -> None:
        self.dismiss(n)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class DetailScreen(ModalScreen[None]):
    """Modal screen showing full card details."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "app.pop_screen", "Close")]

    # The panel is the scroll container and sizes to its content, so a short
    # card is a short box. Its background matches the screen: a different fill
    # would show as a square rim around the rounded border.
    DEFAULT_CSS = """
        DetailScreen {
            align: center middle;
            background: $background 70%;
        }
        DetailScreen #detail-panel {
            width: 80%;
            min-width: 48;
            max-width: 100;
            height: auto;
            max-height: 90%;
            background: $background;
            border: round $primary;
            border-title-color: $primary;
            border-title-style: bold;
            border-subtitle-color: $text-muted;
            padding: 1 2;
            scrollbar-size: 1 1;
            scrollbar-color: $border;
            scrollbar-background: $background;
        }
    """

    def __init__(self, title: str, body: RenderableType) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail-panel"):
            yield Static(self._body)

    def on_mount(self) -> None:
        panel = self.query_one("#detail-panel")
        panel.border_title = self._title
        panel.border_subtitle = "esc to close"


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

    CSS = (
        PALETTE_CSS
        + """
    Screen {
        background: $background;
    }
    /* One docked container per edge: docking two widgets to the same edge
       makes them overlap (#78), so the bars live inside #top in normal flow. */
    /* Footer colours are set here, not left to the theme's footer-* variables,
       which assume a footer on a $primary background. */
    Footer {
        background: $background;
        padding: 0 1;
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
        background: $background;
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
    /* One background for the whole table: a filled header and zebra rows read
       as stripes against the flat panes around it. The cursor row is the only
       highlight. */
    DataTable > .datatable--header {
        background: $background;
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
        background: $background;
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
    )

    # Footer renders the ``show=True`` entries for the focused widget, so this
    # list is also the on-screen key legend. ``?`` opens the full list (#7).
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("slash", "focus_search", "Filter", key_display="/", show=True),
        Binding("space", "toggle_mark", "Mark", key_display="Space", show=True),
        Binding("a", "mark_all", "Mark all", show=False),
        Binding("s", "suspend_selected", "Suspend", show=True),
        Binding("b", "bury_selected", "Bury", show=False),
        Binding("f", "flag_selected", "Flag", show=True),
        Binding("m", "move_selected", "Move to deck", show=False),
        Binding("t", "tag_selected", "Edit tags", show=False),
        Binding("R", "reschedule_selected", "Reschedule", show=False),
        Binding("d", "delete_selected", "Delete", show=True),
        Binding("o", "open_in_review", "Review card", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("left_square_bracket", "prev_filter", "Prev tab", key_display="[", show=False),
        Binding("right_square_bracket", "next_filter", "Next tab", key_display="]", show=False),
        Binding("1", "focus_pane('table')", "Cards", show=False),
        Binding("2", "focus_pane('preview-scroll')", "Preview", show=False),
        Binding("p", "toggle_preview", "Preview on/off", show=False),
        Binding("z", "toggle_zoom", "Zoom pane", show=False),
        # Quit before Help: at 80 columns the footer's tail is what gets cut off.
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "show_help_panel", "Help", key_display="?", show=True),
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
        # Cards ticked with Space; the batch actions apply to these when present.
        self._marked: set[int] = set()
        # Highlighted card to put the cursor back on after a mutation reloads.
        self._keep_card_id: int | None = None
        self._backend = backend
        self._query = query
        self._cards: list[dict[str, Any]] = []
        self._visible_cards: list[dict[str, Any]] = []
        self._active_filter = "all"
        self._last_cursor_row = -999
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
            with Vertical(id="preview"), PreviewScroll(id="preview-scroll"):
                yield Static("", id="preview-body")
                yield Static("", id="preview-meta")
        yield Footer(compact=True)

    # Row actions are pointless (and clutter the footer) with nothing selected.
    _ROW_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "toggle_mark",
            "mark_all",
            "suspend_selected",
            "bury_selected",
            "flag_selected",
            "move_selected",
            "tag_selected",
            "reschedule_selected",
            "delete_selected",
            "open_in_review",
        }
    )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # These are app-level bindings, so a screen pushed on top (review) would
        # still receive them: `d` there must not delete the card under it.
        if isinstance(self.screen, ReviewScreen) and action in self._own_actions():
            return False
        return not (action in self._ROW_ACTIONS and not self._visible_cards)

    @classmethod
    def _own_actions(cls) -> frozenset[str]:
        """The action names of this app's own key bindings, minus the generic
        ones that every screen may use."""
        names = {b.action.split("(")[0] for b in cls.BINDINGS if isinstance(b, Binding)}
        return frozenset(names - {"quit", "show_help_panel"})

    def app_commands(self) -> Iterable[SystemCommand]:
        yield command("Open card details", "Enter", self.action_show_detail)
        yield command("Review this card", "o", self.action_open_in_review)
        yield command("Search cards", "/", self.action_focus_search)
        yield command("Mark / unmark card", "Space", self.action_toggle_mark)
        yield command("Mark all cards", "a", self.action_mark_all, common=False)
        yield command("Suspend / unsuspend", "s", self.action_suspend_selected)
        yield command("Bury / unbury", "b", self.action_bury_selected, common=False)
        yield command("Set flag", "f", self.action_flag_selected)
        yield command("Move to deck", "m", self.action_move_selected)
        yield command("Edit tags", "t", self.action_tag_selected)
        yield command("Reschedule", "R", self.action_reschedule_selected, common=False)
        yield command("Delete note", "d", self.action_delete_selected)
        yield command("Toggle preview", "p", self.action_toggle_preview, common=False)
        # Found by typing; keeps the opening list short.
        for key in self._FILTER_ORDER:
            yield command(
                f"Filter: {self._FILTER_LABELS[key]}",
                None,
                lambda key=key: self._set_filter(key),
                common=False,
            )
        yield command("Zoom pane", "z", self.action_toggle_zoom, common=False)
        yield command("Reload cards", "r", self.action_refresh, common=False)

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
        if self._marked:
            self._marked.clear()
            self._populate_table(reset_cursor=False)
            self._refresh_count()
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

    def action_open_in_review(self) -> None:
        card = self._selected_card()
        cid = self._cid(card) if card else 0
        if card is None or cid <= 0:
            self._toast("No card selected", severity="warning")
            return
        if not getattr(self._backend, "supports_scheduler_introspection", False):
            self._toast("Reviewing needs the direct backend (--backend direct)", severity="warning")
            return

        def back(_: None) -> None:
            # The answer changed the card, so show it as it is now.
            self._reload()

        self.push_screen(ReviewScreen(backend=self._backend, deck=None, card_id=cid), back)

    def action_show_detail(self) -> None:
        row_idx = self._selected_row_index()
        if row_idx >= 0:
            self._show_detail_for_row(row_idx)

    # -- selection ---------------------------------------------------------------

    @staticmethod
    def _cid(card: Mapping[str, Any]) -> int:
        return to_int(card.get("cardId"), 0)

    def _targets(self) -> list[dict[str, Any]]:
        """The ticked cards (in table order), else the highlighted one."""
        if self._marked:
            ticked = [c for c in self._visible_cards if self._cid(c) in self._marked]
            if ticked:
                return ticked
        card = self._selected_card()
        return [card] if card is not None else []

    def _target_ids(self) -> list[int]:
        return [cid for cid in (self._cid(c) for c in self._targets()) if cid > 0]

    def _note_ids(self, cards: Sequence[Mapping[str, Any]]) -> list[int]:
        seen: dict[int, None] = {}
        for card in cards:
            note_id = _extract_note_id_from_card(card)
            if note_id is not None:
                seen[note_id] = None
        return list(seen)

    def action_toggle_mark(self) -> None:
        card = self._selected_card()
        if card is None or self._cid(card) <= 0:
            return
        cid = self._cid(card)
        self._marked.symmetric_difference_update({cid})
        table = self.query_one("#table", DataTable)
        row = self._selected_row_index()
        with contextlib.suppress(Exception):
            cell = _format_browser_row(
                card,
                question_width=self._current_question_width,
                pal=self.palette,
                marked=cid in self._marked,
            )[0]
            table.update_cell_at(Coordinate(row, 0), cell)
        self._refresh_count()
        # Tick and move on, so a run of Space presses marks a run of cards.
        with contextlib.suppress(Exception):
            table.action_cursor_down()

    def action_mark_all(self) -> None:
        ids = {self._cid(c) for c in self._visible_cards if self._cid(c) > 0}
        self._marked = set() if ids and ids <= self._marked else ids
        self._populate_table(reset_cursor=False)
        self._refresh_count()

    def _reload(self) -> None:
        """Reload after a change, keeping the cursor on the same card."""
        card = self._selected_card()
        self._keep_card_id = self._cid(card) if card else None
        self._load_cards(self._current_query())

    def _no_selection(self) -> bool:
        if self._targets():
            return False
        self._toast("No card selected", severity="warning")
        return True

    @staticmethod
    def _cards_word(n: int) -> str:
        return f"{n} card{'s' if n != 1 else ''}"

    # -- suspend / bury / flag ---------------------------------------------------

    def action_suspend_selected(self) -> None:
        if self._no_selection():
            return
        targets = self._targets()
        # Anki's rule: if everything is already suspended, unsuspend; else suspend.
        unsuspend = all(to_int(c.get("queue"), 0) == -1 for c in targets)
        self._suspend_cards(self._target_ids(), unsuspend)

    @work(thread=True, group="mutate")
    def _suspend_cards(self, card_ids: list[int], unsuspend: bool) -> None:
        # Off the UI thread: the backend call opens/locks the collection.
        try:
            if unsuspend:
                self._backend.unsuspend_cards(card_ids)
            else:
                self._backend.suspend_cards(card_ids)
        except Exception as exc:
            self._toast(f"Suspend failed: {exc}", severity="error")
            return

        done = "unsuspended" if unsuspend else "suspended"
        what = f"Card {card_ids[0]}" if len(card_ids) == 1 else self._cards_word(len(card_ids))
        self._toast(f"{what} {done}")
        self.call_from_thread(self._reload)

    def action_bury_selected(self) -> None:
        if self._no_selection():
            return
        targets = self._targets()
        unbury = all(to_int(c.get("queue"), 0) in (-2, -3) for c in targets)
        self._bury_cards(self._target_ids(), unbury)

    @work(thread=True, group="mutate")
    def _bury_cards(self, card_ids: list[int], unbury: bool) -> None:
        try:
            if unbury:
                self._backend.unbury_cards(card_ids=card_ids)
            else:
                self._backend.bury_cards(card_ids)
        except Exception as exc:
            self._toast(f"{'Unbury' if unbury else 'Bury'} failed: {exc}", severity="error")
            return
        done = "unburied" if unbury else "buried"
        what = f"Card {card_ids[0]}" if len(card_ids) == 1 else self._cards_word(len(card_ids))
        self._toast(f"{what} {done}")
        self.call_from_thread(self._reload)

    def action_flag_selected(self) -> None:
        if self._no_selection():
            return
        ids = self._target_ids()
        targets = self._targets()

        def done(flag: int | None) -> None:
            if flag is not None:
                self._set_flag(ids, flag)

        self.push_screen(FlagScreen(_flag_of(targets[0]), self.palette), done)

    @work(thread=True, group="mutate")
    def _set_flag(self, card_ids: list[int], flag: int) -> None:
        try:
            self._backend.set_card_flag(card_ids, flag)
        except Exception as exc:
            self._toast(f"Flag failed: {exc}", severity="error")
            return
        what = f"Card {card_ids[0]}" if len(card_ids) == 1 else self._cards_word(len(card_ids))
        name = _FLAGS[flag][0]
        self._toast(f"{what}: {name.lower()}" if flag else f"{what}: flag cleared")
        self.call_from_thread(self._reload)

    # -- move / tags / reschedule: typed values ----------------------------------

    def action_move_selected(self) -> None:
        if self._no_selection():
            return
        self._ask_move(self._target_ids())

    @work(thread=True, group="mutate")
    def _ask_move(self, card_ids: list[int]) -> None:
        try:
            names = [str(d.get("name", "")) for d in self._backend.get_decks() if d.get("name")]
        except Exception as exc:
            self._toast(f"Could not list decks: {exc}", severity="error")
            return
        self.call_from_thread(self._show_move_prompt, card_ids, names)

    def _show_move_prompt(self, card_ids: list[int], names: list[str]) -> None:
        by_lower = {n.lower(): n for n in names}

        def validate(text: str) -> str | None:
            if not text:
                return "Type a deck name."
            return None if text.lower() in by_lower else f"No deck called '{text}'."

        def done(text: str | None) -> None:
            if text:
                self._move_cards(card_ids, by_lower[text.lower()])

        self.push_screen(
            PromptScreen(
                f"Move {self._cards_word(len(card_ids))}",
                "Deck to move to (the deck must already exist).",
                placeholder="Deck::Subdeck",
                suggester=TokenSuggester(names, multi=False),
                validate=validate,
            ),
            done,
        )

    @work(thread=True, group="mutate")
    def _move_cards(self, card_ids: list[int], deck: str) -> None:
        try:
            self._backend.move_cards(card_ids, deck)
        except Exception as exc:
            self._toast(f"Move failed: {exc}", severity="error")
            return
        self._toast(f"Moved {self._cards_word(len(card_ids))} to {deck}")
        self.call_from_thread(self._reload)

    def action_tag_selected(self) -> None:
        if self._no_selection():
            return
        notes = self._note_ids(self._targets())
        if not notes:
            self._toast("Selected cards have no note id", severity="error")
            return
        self._ask_tags(notes)

    @work(thread=True, group="mutate")
    def _ask_tags(self, note_ids: list[int]) -> None:
        try:
            known = [str(t) for t in self._backend.get_tags()]
        except Exception:
            known = []
        self.call_from_thread(self._show_tag_prompt, note_ids, known)

    def _show_tag_prompt(self, note_ids: list[int], known: list[str]) -> None:
        def validate(text: str) -> str | None:
            return None if text else "Type at least one tag."

        def done(text: str | None) -> None:
            if not text:
                return
            words = text.split()
            add = [w for w in words if not w.startswith("-") and w]
            remove = [w[1:] for w in words if w.startswith("-") and len(w) > 1]
            if add or remove:
                self._apply_tags(note_ids, add, remove)

        notes = f"{len(note_ids)} note{'s' if len(note_ids) != 1 else ''}"
        self.push_screen(
            PromptScreen(
                f"Tags for {notes}",
                "Tags to add, space separated. Put - in front of one to remove it.",
                placeholder="verb -old-tag",
                suggester=TokenSuggester(known),
                validate=validate,
            ),
            done,
        )

    @work(thread=True, group="mutate")
    def _apply_tags(self, note_ids: list[int], add: list[str], remove: list[str]) -> None:
        try:
            if add:
                self._backend.add_tags(note_ids, add)
            if remove:
                self._backend.remove_tags(note_ids, remove)
        except Exception as exc:
            self._toast(f"Tagging failed: {exc}", severity="error")
            return
        parts = [f"+{t}" for t in add] + [f"-{t}" for t in remove]
        notes = f"{len(note_ids)} note{'s' if len(note_ids) != 1 else ''}"
        self._toast(f"{notes}: {' '.join(parts)}")
        self.call_from_thread(self._reload)

    def action_reschedule_selected(self) -> None:
        if self._no_selection():
            return
        ids = self._target_ids()

        def validate(text: str) -> str | None:
            if not text.isdigit():
                return "Enter a whole number of days (0 or more)."
            return None

        def done(text: str | None) -> None:
            if text:
                self._reschedule_cards(ids, int(text))

        self.push_screen(
            PromptScreen(
                f"Reschedule {self._cards_word(len(ids))}",
                "Days from today until they are next due (0 = due today).",
                placeholder="7",
                validate=validate,
            ),
            done,
        )

    @work(thread=True, group="mutate")
    def _reschedule_cards(self, card_ids: list[int], days: int) -> None:
        try:
            self._backend.reschedule_cards(card_ids, days)
        except Exception as exc:
            self._toast(f"Reschedule failed: {exc}", severity="error")
            return
        when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
        self._toast(f"{self._cards_word(len(card_ids))} due {when}")
        self.call_from_thread(self._reload)

    # -- delete -----------------------------------------------------------------

    def action_delete_selected(self) -> None:
        if self._no_selection():
            return
        notes = self._note_ids(self._targets())
        if not notes:
            self._toast("Selected card has no note id", severity="error")
            return
        self._ask_delete(notes)

    @work(thread=True, group="mutate")
    def _ask_delete(self, note_ids: list[int]) -> None:
        # A note owns every card made from it, so say how many go with it.
        try:
            count = sum(len(self._backend.find_cards(f"nid:{n}")) for n in note_ids)
        except Exception:
            wanted = set(note_ids)
            count = sum(1 for c in self._cards if _extract_note_id_from_card(c) in wanted)
        self.call_from_thread(self._confirm_delete, note_ids, count)

    def _confirm_delete(self, note_ids: list[int], count: int) -> None:
        cards = self._cards_word(count) if count else "all their cards"
        if len(note_ids) == 1:
            message = f"Delete note {note_ids[0]} and {cards}?\n\n"
        else:
            message = f"Delete {len(note_ids)} notes and {cards}?\n\n"
        message += "Every card made from them is removed. This can't be undone."

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self._delete_notes(note_ids)

        title = "Delete note" if len(note_ids) == 1 else f"Delete {len(note_ids)} notes"
        self.push_screen(ConfirmScreen(title, message), done)

    @work(thread=True, group="mutate")
    def _delete_notes(self, note_ids: list[int]) -> None:
        try:
            result = self._backend.delete_notes(note_ids)
        except Exception as exc:
            self._toast(f"Delete failed: {exc}", severity="error")
            return

        deleted_notes = 0
        if isinstance(result, Mapping):
            deleted_notes = to_int(result.get("deleted_notes"), 0)
            if deleted_notes == 0:
                deleted_notes = to_int(result.get("deleted"), 0)

        if deleted_notes > 0:
            what = f"note {note_ids[0]}" if len(note_ids) == 1 else f"{deleted_notes} notes"
            self._toast(f"Deleted {what}")
        else:
            self._toast("Nothing was deleted", severity="warning")

        self._marked.difference_update(set(self._target_ids_for_notes(note_ids)))
        self.call_from_thread(self._reload)

    def _target_ids_for_notes(self, note_ids: list[int]) -> list[int]:
        wanted = set(note_ids)
        return [self._cid(c) for c in self._cards if _extract_note_id_from_card(c) in wanted]

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
        label = f"{len(self._visible_cards)}/{len(self._cards)} cards"
        marked = len(self._marked & {self._cid(c) for c in self._visible_cards})
        return f"{marked} selected \u00b7 {label}" if marked else label

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
        meta.append(" Deck     ", style=pal.muted)
        meta.append(f"{card.get('deckName') or '-'}\n", style=pal.accent)
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

    def _refresh_count(self) -> None:
        """Re-render the header's count (it includes how many cards are ticked)."""
        self._set_status(self._header_state[0], self._count_label())

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
        # Ticks survive a reload for cards that are still there.
        self._marked &= {self._cid(card) for card in self._cards}
        self._populate_table(reset_cursor=True)
        self._restore_cursor()
        self._render_filter_tabs()
        self._sync_preview_cursor(force=True)
        self._header_state = (query or "*", self._count_label())
        self._set_status(query or "*", self._count_label())
        if self.focused is None:
            with contextlib.suppress(Exception):
                self.query_one("#table", DataTable).focus()

    def _restore_cursor(self) -> None:
        """After a mutation reload, put the cursor back on the card it was on."""
        wanted, self._keep_card_id = self._keep_card_id, None
        if wanted is None:
            return
        for index, card in enumerate(self._visible_cards):
            if self._cid(card) == wanted:
                with contextlib.suppress(Exception):
                    self.query_one("#table", DataTable).move_cursor(row=index, column=0)
                return

    def _populate_table(self, *, reset_cursor: bool = False) -> None:
        table = self.query_one("#table", DataTable)
        # clear() sends the cursor back to the top; remember where it was.
        previous_row = to_int(getattr(table, "cursor_row", -1), -1)
        table.clear(columns=True)
        self._add_columns()
        pal = self.palette
        for card in self._visible_cards:
            table.add_row(
                *_format_browser_row(
                    card,
                    question_width=self._current_question_width,
                    pal=pal,
                    marked=self._cid(card) in self._marked,
                )
            )

        if self._visible_cards:
            row = (
                0
                if reset_cursor or previous_row < 0
                else min(previous_row, len(self._visible_cards) - 1)
            )
            with contextlib.suppress(Exception):
                table.move_cursor(row=row, column=0)
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
