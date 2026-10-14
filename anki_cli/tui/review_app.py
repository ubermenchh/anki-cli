from __future__ import annotations

import contextlib
import html as _html
import re
import shlex
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Static
from textual.worker import get_current_worker

from anki_cli import __version__
from anki_cli.core.render import extract_note_id, extract_ord, pick_template, render_card
from anki_cli.core.review_session import ReviewSession
from anki_cli.tui._utils import due_day_label, relative_eta, to_int
from anki_cli.tui.theming import DEFAULT_PALETTE, PALETTE_CSS, Palette, ThemedApp, command

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_HR_RE = re.compile(r"(?i)<hr[^>]*>")


def _strip_html_basic(value: str) -> str:
    # Good enough for terminal UI: keep line breaks, drop tags.
    text = _BR_RE.sub("\n", value)
    text = _HR_RE.sub("\n" + ("-" * 40) + "\n", text)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


# Kept under their old private names: tests import them from here.
_extract_note_id = extract_note_id
_extract_ord = extract_ord


def _pick_template(templates: Mapping[str, Any], ord_: int) -> Mapping[str, Any] | None:
    picked = pick_template(templates, ord_)
    return picked[1] if picked is not None else None


def _format_due_info_short(due_info: Any) -> str:
    if isinstance(due_info, Mapping):
        kind = str(due_info.get("kind") or "")
        if kind == "new_position":
            return "new"
        if kind == "learn_epoch_secs":
            epoch = due_info.get("epoch_secs")
            if isinstance(epoch, int):
                return relative_eta(epoch)
            return "learn"
        if kind in ("review_day_index", "learn_day_index"):
            return due_day_label(due_info)
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

    DEFAULT_CSS = """
    PreviewScreen {
        align: center middle;
        background: $background 70%;
    }

    PreviewScreen #preview-panel {
        width: 84%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $border;
        padding: 1 2;
    }

    PreviewScreen #preview-title {
        height: auto;
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
        border-bottom: solid $border;
    }

    PreviewScreen #preview-body {
        height: 1fr;
        margin-top: 1;
        background: $background;
        border: round $border;
        padding: 1;
    }

    PreviewScreen #preview-content {
        color: $foreground;
    }

    PreviewScreen #preview-hint {
        height: auto;
        color: $text-muted;
        text-align: right;
        padding-top: 1;
    }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-panel"):
            yield Static(self._title, id="preview-title")
            with VerticalScroll(id="preview-body"):
                yield Static(Text(self._body), id="preview-content")
            yield Static("esc/q to close", id="preview-hint")


@dataclass
class _Loaded:
    """Everything one card needs on screen, fetched off the UI thread (#84)."""

    card_id: int | None = None
    kind: str = "none"
    card: dict[str, Any] | None = None
    rendered: tuple[int, str, str] | None = None
    error: str | None = None
    hints: dict[int, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


_RATINGS: tuple[tuple[int, str], ...] = ((1, "Again"), (2, "Hard"), (3, "Good"), (4, "Easy"))
_DEFAULT_HINTS: dict[int, str] = {1: "<10m", 2: "-", 3: "-", 4: "-"}


class ReviewScreen(Screen[None]):
    """The review UI. It is a Screen so browse can push it for one card (``o``);
    ``ReviewApp`` below hosts it for ``anki review:start``."""

    # Textual's default AUTO_FOCUS="*" would focus the first widget on mount.
    # Leaving focus unset keeps Enter from pressing a rating by accident (#81);
    # an empty string (not None) is what disables it on a Screen.
    AUTO_FOCUS = ""

    # Under 30 rows the buttons drop to one row and the card padding goes.
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-short"), (30, "-tall")]

    CSS = """
    ReviewScreen {
        background: $background;
    }
    /* Header, card, buttons and footer all live in one centred column, so they
       share the same edges and the same background. */
    #page {
        width: 100%;
        height: 1fr;
        align-horizontal: center;
    }
    #column {
        width: 100%;
        max-width: 90;
        height: 1fr;
    }
    #header {
        height: 1;
        margin-bottom: 1;
        padding: 0 1;
    }
    #header-title {
        width: auto;
        color: $primary;
        text-style: bold;
    }
    #header-stats {
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }
    #column > Footer {
        dock: none;
        height: 1;
        margin-top: 1;
        padding: 0 1;
        background: $background;
    }
    #column > Footer FooterKey,
    #column > Footer .footer-key--key,
    #column > Footer .footer-key--description {
        background: transparent;
    }
    #column > Footer .footer-key--key {
        color: $primary;
    }
    #column > Footer .footer-key--description {
        color: $text-muted;
    }
    .-no-color #column > Footer .footer-key--description {
        color: $foreground;
    }
    .card-panel {
        height: 1fr;
        min-height: 5;
        background: $background;
        border: round $border;
        border-title-color: $text-muted;
        padding: 1 2;
        scrollbar-size: 1 1;
        scrollbar-color: $border;
        scrollbar-background: $background;
    }
    .card-panel:focus {
        border: round $primary;
        border-title-color: $primary;
        background-tint: transparent;
    }
    #answer-panel {
        display: none;
    }
    #column.-revealed #answer-panel {
        display: block;
    }
    #question, #answer {
        color: $foreground;
    }

    /* One action row that morphs: "Show answer" until the answer is up, then
       the four ratings. */
    #actions {
        height: 3;
        margin-top: 1;
    }
    /* Outlined, rounded buttons to match the panels; the variant colour is the
       outline and the text. A fill would paint a square block behind the
       rounded corners, so hover / focus brighten and underline the label. */
    #actions Button {
        height: 3;
        width: 1fr;
        margin: 0 1 0 0;
        min-width: 8;
        border: round $border;
        background: $background;
        color: $foreground;
        text-style: bold;
        tint: transparent;
    }
    #actions #show-answer {
        border: round $primary;
        color: $primary;
    }
    #actions #show-answer:hover,
    #actions #show-answer:focus,
    #actions #rate-1:hover,
    #actions #rate-1:focus,
    #actions #rate-2:hover,
    #actions #rate-2:focus,
    #actions #rate-3:hover,
    #actions #rate-3:focus,
    #actions #rate-4:hover,
    #actions #rate-4:focus {
        color: $foreground;
        text-style: bold underline;
        background: $background;
        background-tint: transparent;
    }
    #actions #rate-1 { border: round $error; color: $error; }
    #actions #rate-2 { border: round $warning; color: $warning; }
    #actions #rate-3 { border: round $success; color: $success; }
    #actions #rate-4 { border: round $primary; color: $primary; }
    #rate-row {
        display: none;
        height: 3;
    }
    #rate-row Button:last-of-type {
        margin-right: 0;
    }
    #column.-revealed #rate-row {
        display: block;
    }
    #column.-revealed #show-answer {
        display: none;
    }
    #show-answer {
        margin-right: 0;
    }

    #cmd-slot {
        height: auto;
    }
    #cmd {
        height: 1;
        border: none;
        background: $surface;
        color: $foreground;
        padding: 0 1;
    }

    ReviewScreen.-short #header {
        margin-bottom: 0;
    }
    ReviewScreen.-short #column > Footer {
        margin-top: 0;
    }
    ReviewScreen.-short .card-panel {
        padding: 0 1;
        min-height: 3;
    }
    ReviewScreen.-short #actions, ReviewScreen.-short #rate-row {
        height: 1;
        margin-top: 0;
    }
    ReviewScreen.-short #actions Button {
        height: 1;
        border: none;
    }
    ReviewScreen.-short #actions #show-answer {
        border: none;
        height: 1;
        background: $primary 20%;
        color: $primary;
    }
    ReviewScreen.-short #actions #rate-1 {
        border: none;
        height: 1;
        background: $error 20%;
        color: $error;
    }
    ReviewScreen.-short #actions #rate-2 {
        border: none;
        height: 1;
        background: $warning 20%;
        color: $warning;
    }
    ReviewScreen.-short #actions #rate-3 {
        border: none;
        height: 1;
        background: $success 20%;
        color: $success;
    }
    ReviewScreen.-short #actions #rate-4 {
        border: none;
        height: 1;
        background: $primary 20%;
        color: $primary;
    }
    ReviewScreen.-short #actions #show-answer:hover,
    ReviewScreen.-short #actions #show-answer:focus,
    ReviewScreen.-short #actions #rate-1:hover,
    ReviewScreen.-short #actions #rate-1:focus,
    ReviewScreen.-short #actions #rate-2:hover,
    ReviewScreen.-short #actions #rate-2:focus,
    ReviewScreen.-short #actions #rate-3:hover,
    ReviewScreen.-short #actions #rate-3:focus,
    ReviewScreen.-short #actions #rate-4:hover,
    ReviewScreen.-short #actions #rate-4:focus {
        color: $foreground;
        text-style: bold underline;
    }
    """

    # Footer renders the ``show=True`` entries, so this list is also the
    # on-screen key legend; ``?`` opens the full list. 1-4 are shown on the
    # buttons themselves.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "toggle_answer", "Flip", key_display="Space", show=True),
        Binding("1", "rate(1)", "Again", show=False),
        Binding("2", "rate(2)", "Hard", show=False),
        Binding("3", "rate(3)", "Good", show=False),
        Binding("4", "rate(4)", "Easy", show=False),
        Binding("u", "undo", "Undo", show=True),
        Binding("p", "preview", "Preview", show=True),
        Binding("n", "next", "Next", show=True),
        Binding(":", "focus_command", "Command", show=True),
        Binding("escape", "blur_command", "Blur command", show=False),
        Binding("question_mark", "app.show_help_panel", "Help", key_display="?", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        *,
        backend: Any,
        deck: str | None,
        card_id: int | None = None,
        standalone: bool = False,
    ) -> None:
        """``card_id`` reviews that one card and then leaves (opened from browse);
        ``standalone`` is set by the host app, which exits instead of popping."""
        super().__init__()
        self._single_card_id = card_id
        self._standalone = standalone
        self._backend = backend
        # Undo, preview and the scheduler-ordered picker need scheduler state
        # only the direct backend exposes (AnkiBackend.supports_scheduler_introspection).
        # Next-card choice, the answer + undo flow, undo and previews live in the
        # shared session (#36); this app only decides how to show them.
        self._session = ReviewSession(backend, deck)
        self._introspects = self._session.introspects
        self._card_id: int | None = None
        self._kind: str = "none"
        self._show_answer = False

        self._current_card: dict[str, Any] | None = None
        # (card id, question, answer) rendered once per card; flipping the
        # answer repaints from this instead of re-fetching card, note and
        # notetype on every keypress (#35).
        self._rendered: tuple[int, str, str] | None = None
        self._hints: dict[int, str] = dict(_DEFAULT_HINTS)
        self._counts: dict[str, int] = {"new": 0, "learn": 0, "review": 0, "total": 0}
        self._loading = False
        self._session_total = 0
        self._answered = 0
        self._rating_counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}

    @property
    def palette(self) -> Palette:
        """The active theme's colours (the default look when not mounted)."""
        try:
            return cast("ThemedApp", self.app).palette
        except Exception:
            return DEFAULT_PALETTE

    def _push(self, screen: Screen[Any]) -> None:
        self.app.push_screen(screen)

    def _leave(self) -> None:
        """Quit when hosting the app; otherwise go back to whoever pushed us."""
        if self._standalone:
            self.app.exit()
        else:
            self.dismiss()

    @property
    def _single_card(self) -> bool:
        return self._single_card_id is not None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # "Next card" would leave the one card this screen was opened for.
        return not (action == "next" and self._single_card)

    @property
    def _deck(self) -> str | None:
        return self._session.deck

    @_deck.setter
    def _deck(self, value: str | None) -> None:
        self._session.deck = value

    def compose(self) -> ComposeResult:
        with Vertical(id="page"), Vertical(id="column"):
            with Horizontal(id="header"):
                yield Static(Text(f"anki-cli {__version__} · study"), id="header-title")
                yield Static("", id="header-stats")

            with VerticalScroll(id="question-panel", classes="card-panel"):
                yield Static("", id="question")
            with VerticalScroll(id="answer-panel", classes="card-panel"):
                yield Static("", id="answer")
            with Vertical(id="actions"):
                yield Button("Show answer (Space)", id="show-answer", variant="default")
                with Horizontal(id="rate-row"):
                    yield Button("1 Again", id="rate-1", classes="rate-btn", variant="error")
                    yield Button("2 Hard", id="rate-2", classes="rate-btn", variant="warning")
                    yield Button("3 Good", id="rate-3", classes="rate-btn", variant="success")
                    yield Button("4 Easy", id="rate-4", classes="rate-btn", variant="primary")

            # Mounted by ":" and removed again, so it is not a permanent bottom bar.
            yield Vertical(id="cmd-slot")
            yield Footer(compact=True)

    def app_commands(self) -> Iterable[SystemCommand]:
        yield command("Flip card", "Space", self.action_toggle_answer)
        for ease, name in _RATINGS:
            yield command(f"Answer: {name}", str(ease), lambda ease=ease: self.action_rate(ease))
        yield command("Undo last answer", "u", self.action_undo)
        yield command("Preview ratings", "p", self.action_preview)
        # Found by typing; keeps the opening list short.
        yield command("Skip to next card", "n", self.action_next, common=False)
        yield command("Command line", ":", self.action_focus_command, common=False)

    def on_palette_changed(self) -> None:
        self._refresh_chrome()

    def on_mount(self) -> None:
        self.query_one("#question-panel").border_title = "Question"
        self.query_one("#answer-panel").border_title = "Answer"
        self._refresh_rate_buttons()
        self._refresh_chrome()
        if self._single_card_id is not None:
            self._load_next(card_id=self._single_card_id, kind="browse")
        else:
            self._load_next()

    async def action_quit(self) -> None:
        self._leave()

    def action_toggle_answer(self) -> None:
        if self._card_id is None or self._loading:
            return
        self._show_answer = not self._show_answer
        self._render_current()

    def action_next(self) -> None:
        self._load_next()

    @on(Button.Pressed, "#show-answer")
    def _on_show_answer(self) -> None:
        self.action_toggle_answer()

    @on(Button.Pressed, ".rate-btn")
    def _on_rate_pressed(self, event: Button.Pressed) -> None:
        self.action_rate(int((event.button.id or "rate-0").rsplit("-", 1)[1]))

    def action_rate(self, ease: int) -> None:
        if self._card_id is None or self._loading:
            return

        # Require showing the answer before rating (closer to Anki UX).
        if not self._show_answer:
            self._show_answer = True
            self._render_current()
            return

        try:
            outcome = self._session.answer(int(self._card_id), int(ease))
        except Exception as exc:
            # A locked collection or a vanished card surfaces here, not as a
            # Textual traceback that exits 0 (#83).
            self._set_status(f"answer failed: {exc}")
            return

        if outcome.undo_error:
            self._set_status(f"answered; undo not saved: {outcome.undo_error}")

        self._answered += 1
        self._rating_counts[int(ease)] = self._rating_counts.get(int(ease), 0) + 1
        if self._single_card:
            self._leave()
            return
        self._load_next()

    def action_undo(self) -> None:
        if not self._introspects:
            self._set_status("undo is supported only for direct backend")
            return

        try:
            outcome = self._session.undo()
        except Exception as exc:
            self._set_status(f"undo failed: {exc}")
            return
        if outcome is None:
            self._set_status("undo empty")
            return

        if self._answered > 0:
            self._answered -= 1

        self._set_status("undone")
        # The restored card's state changed, so it is re-fetched and re-rendered.
        self._load_next(card_id=int(outcome.item.card_id), kind="undo")

    def action_preview(self) -> None:
        if self._card_id is None:
            return
        if not self._introspects:
            self._set_status("preview is supported only for direct backend")
            return

        try:
            items = self._backend.preview_ratings(int(self._card_id))
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
        self._push(PreviewScreen(f"Preview ratings - card {self._card_id}", body))

    async def action_focus_command(self) -> None:
        existing = self.query("#cmd")
        if existing:
            existing.first(Input).focus()
            return
        cmd = Input(value=":", placeholder=":help", id="cmd")
        await self.query_one("#cmd-slot").mount(cmd)
        cmd.focus()
        cmd.cursor_position = len(cmd.value)

    async def action_blur_command(self) -> None:
        await self._close_command()

    async def _close_command(self) -> None:
        for cmd in self.query("#cmd"):
            await cmd.remove()
        self.set_focus(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        await self._close_command()

        if not raw:
            return

        line = raw[1:].strip() if raw.startswith(":") else raw
        self._run_command(line)

    def _ui_update(self, selector: str, value: str | Text) -> None:
        # Static.update(str) parses Textual markup, so card text containing
        # "[x]", "[mg]", "[/]" lost words or raised MarkupError (#82). Plain
        # strings are wrapped so they render verbatim; chrome that wants
        # styling passes a Text.
        if isinstance(value, str):
            value = Text(value)
        with contextlib.suppress(Exception):
            self.query_one(selector, Static).update(value)

    def _set_status(self, msg: str) -> None:
        """Toast an action result; failures are shown as errors."""
        lowered = msg.lower()
        if "failed" in lowered or "not saved" in lowered:
            severity = "error"
        elif lowered.startswith(("unknown command", "parse error", "usage", "undo empty")) or (
            "only for direct" in lowered
        ):
            severity = "warning"
        else:
            severity = "information"
        self._toast(msg, severity=severity)

    def _toast(self, message: str, *, severity: str = "information") -> None:
        if threading.current_thread() is threading.main_thread():
            self.app.notify(message, severity=severity, markup=False)  # type: ignore[arg-type]
        else:
            with contextlib.suppress(Exception):
                self.app.call_from_thread(self.app.notify, message, severity=severity, markup=False)

    @staticmethod
    def _due_counts(backend: Any, deck: str | None) -> dict[str, int]:
        empty = {"new": 0, "learn": 0, "review": 0, "total": 0}
        try:
            raw = backend.get_due_counts(deck=deck)
        except Exception:
            return empty
        if not isinstance(raw, Mapping):
            return empty

        new_count = to_int(raw.get("new"), 0)
        learn_count = to_int(raw.get("learn"), 0)
        review_count = to_int(raw.get("review"), 0)
        total_count = to_int(raw.get("total"), new_count + learn_count + review_count)
        return {
            "new": new_count,
            "learn": learn_count,
            "review": review_count,
            "total": total_count,
        }

    def _safe_due_counts(self) -> dict[str, int]:
        return self._due_counts(self._backend, self._deck)

    @staticmethod
    def _rating_hints_for(backend: Any, introspects: bool, card_id: int | None) -> dict[int, str]:
        hints = dict(_DEFAULT_HINTS)
        if card_id is None or not introspects:
            return hints

        try:
            items = backend.preview_ratings(int(card_id))
        except Exception:
            return hints

        for item in items:
            if not isinstance(item, Mapping):
                continue
            ease = to_int(item.get("ease"), 0)
            if ease not in hints:
                continue
            label = _format_due_info_short(item.get("due_info"))
            if label in ("?", "-"):
                # No decodable due_info: fall back to the raw interval rather
                # than showing "?" on the button now that the row is visible.
                ivl = to_int(item.get("interval"), 0)
                label = f"{ivl}d" if ivl > 0 else "-"
            hints[ease] = label

        return hints

    def _rating_hints(self) -> dict[int, str]:
        return self._rating_hints_for(self._backend, self._introspects, self._card_id)

    def _refresh_rate_buttons(self) -> None:
        for ease, name in _RATINGS:
            with contextlib.suppress(Exception):
                self.query_one(
                    f"#rate-{ease}", Button
                ).label = f"{ease} {name} · {self._hints.get(ease, '-')}"

    def _refresh_chrome(self) -> None:
        counts = self._counts
        remaining_total = counts["total"]

        if self._session_total <= 0:
            self._session_total = remaining_total

        done = max(max(0, self._session_total - remaining_total), self._answered)

        if self._session_total > 0:
            pct = int((done * 100) / self._session_total)
        else:
            pct = 100 if remaining_total == 0 else 0
        pct = max(0, min(100, pct))

        deck_name = self._deck or "all decks"
        title = Text()
        title.append(f"anki-cli {__version__} · study · {deck_name}")
        self._ui_update("#header-title", title)

        pal = self.palette
        stats = Text()
        stats.append(f"{done}/{self._session_total} · {pct}%   ", style=pal.muted)
        stats.append(f"new {counts['new']}", style=f"bold {pal.primary}")
        stats.append("  ")
        stats.append(f"learn {counts['learn']}", style=f"bold {pal.warning}")
        stats.append("  ")
        stats.append(f"review {counts['review']}", style=f"bold {pal.success}")
        self._ui_update("#header-stats", stats)

    def _fetch_next(self, card_id: int | None, kind: str) -> _Loaded:
        """Pick and fully prepare the next card. Runs on a worker thread."""
        loaded = _Loaded()
        try:
            if card_id is not None:
                loaded.card_id, loaded.kind = int(card_id), kind
            else:
                picked = self._session.next()
                loaded.card_id, loaded.kind = picked.card_id, picked.kind
        except Exception as exc:
            loaded.card_id, loaded.kind = None, "error"
            loaded.error = f"failed to pick next card: {exc}"
            return loaded

        loaded.counts = self._safe_due_counts()
        if loaded.card_id is None:
            return loaded

        with contextlib.suppress(Exception):
            card_obj = self._backend.get_card(int(loaded.card_id))
            if isinstance(card_obj, Mapping):
                loaded.card = dict(card_obj)

        try:
            rendered = self._render_card(loaded.card_id, reveal_answer=True)
            loaded.rendered = (
                loaded.card_id,
                _strip_html_basic(str(rendered.get("question") or "")),
                _strip_html_basic(str(rendered.get("answer") or "")),
            )
        except Exception as exc:
            loaded.error = f"render failed: {exc}"
        loaded.hints = self._rating_hints_for(self._backend, self._introspects, loaded.card_id)
        return loaded

    def _load_next(self, card_id: int | None = None, kind: str = "next") -> None:
        # Hide the previous card's controls while the next one loads, so a
        # second keypress cannot answer the card that was just rated.
        self._loading = True
        self._card_id = None
        self._load_worker(card_id, kind)

    @work(thread=True, exclusive=True, group="load")
    def _load_worker(self, card_id: int | None, kind: str) -> None:
        loaded = self._fetch_next(card_id, kind)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply_loaded, loaded)

    def _apply_loaded(self, loaded: _Loaded) -> None:
        self._loading = False
        self._card_id = loaded.card_id
        self._kind = loaded.kind
        self._show_answer = False
        self._current_card = loaded.card
        self._rendered = loaded.rendered
        self._counts = loaded.counts or self._counts
        self._hints = loaded.hints or dict(_DEFAULT_HINTS)

        if loaded.error and loaded.card_id is None:
            self._set_status(loaded.error)
            self._ui_update("#question", "")
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            self._sync_reveal()
            return

        if self._card_id is None:
            deck = self._deck or "(all)"
            self.query_one("#question-panel").border_title = f"Question · {deck}"
            self._ui_update("#question", f"No due cards. deck={deck}")
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            self._sync_reveal()
            return

        if loaded.error:
            self._ui_update("#question", loaded.error)
            self._ui_update("#answer", "")
            self._refresh_rate_buttons()
            self._refresh_chrome()
            self._sync_reveal()
            return

        self._render_current()

    def _sync_reveal(self) -> None:
        """Show the answer block and rating row only once the answer is up."""
        revealed = self._show_answer and self._card_id is not None
        with contextlib.suppress(Exception):
            self.query_one("#column").set_class(revealed, "-revealed")

    def _render_current(self) -> None:
        assert self._card_id is not None
        if self._rendered is None or self._rendered[0] != self._card_id:
            try:
                rendered = self._render_card(self._card_id, reveal_answer=True)
            except Exception as exc:
                self._ui_update("#question", f"render failed: {exc}")
                self._ui_update("#answer", "")
                return
            self._rendered = (
                self._card_id,
                _strip_html_basic(str(rendered.get("question") or "")),
                _strip_html_basic(str(rendered.get("answer") or "")),
            )

        _, question, answer = self._rendered

        self._ui_update("#question", question or "(empty question)")
        self._ui_update("#answer", answer or "(empty answer)")
        self._sync_reveal()

        deck_badge = self._deck
        if not deck_badge and isinstance(self._current_card, Mapping):
            raw_deck = self._current_card.get("deckName")
            if isinstance(raw_deck, str) and raw_deck.strip():
                deck_badge = raw_deck.strip()
        with contextlib.suppress(Exception):
            self.query_one(
                "#question-panel"
            ).border_title = f"Question · {deck_badge or 'all decks'}"

        self._refresh_rate_buttons()
        self._refresh_chrome()

    def _render_card(self, card_id: int, *, reveal_answer: bool) -> dict[str, Any]:
        rendered = render_card(self._backend, int(card_id)).raise_for_error()
        return {"question": rendered.question, "answer": rendered.answer if reveal_answer else ""}

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
            self._push(
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
            self._leave()
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


class ReviewApp(ThemedApp):
    """``anki review:start``: the review screen as an application of its own."""

    CSS = PALETTE_CSS
    AUTO_FOCUS = None

    def __init__(
        self,
        *,
        backend: Any,
        deck: str | None,
        theme: str | None = None,
        no_color: bool = False,
        persist_theme: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(theme=theme, no_color=no_color, persist_theme=persist_theme)
        self.review = ReviewScreen(backend=backend, deck=deck, standalone=True)

    def on_mount(self) -> None:
        self._theme_ready()
        self.push_screen(self.review)
