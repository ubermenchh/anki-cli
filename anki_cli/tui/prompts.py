"""A one-line input dialog with validation and completion, for browse actions
that need a typed value (deck, tags, days) (#86)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Input, Static


class TokenSuggester(Suggester):
    """Completes the last space-separated word from ``choices``.

    ``-`` in front of a word is kept and ignored when matching, so ``-ver``
    completes to ``-verb`` in a "tags to add / -tags to remove" prompt.
    """

    def __init__(self, choices: Sequence[str], *, multi: bool = True) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._choices = list(choices)
        self._multi = multi

    async def get_suggestion(self, value: str) -> str | None:
        if self._multi:
            head, _, word = value.rpartition(" ")
            head = f"{head} " if head or value.startswith(" ") else ""
        else:
            head, word = "", value
        sign = "-" if self._multi and word.startswith("-") else ""
        needle = word[len(sign) :].lower()
        if not needle:
            return None
        for choice in self._choices:
            if choice.lower().startswith(needle) and choice.lower() != needle:
                return f"{head}{sign}{choice}"
        return None


class PromptScreen(ModalScreen[str | None]):
    """Ask for one line of text. Enter submits when ``validate`` (if given)
    returns no error; Esc cancels. The result is the stripped text."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "complete", "Complete", show=False, priority=True),
    ]

    DEFAULT_CSS = """
        PromptScreen {
            align: center middle;
            background: $background 70%;
        }
        PromptScreen #prompt-panel {
            width: 70;
            max-width: 90%;
            height: auto;
            background: $background;
            border: round $primary;
            border-title-color: $primary;
            border-title-style: bold;
            border-subtitle-color: $text-muted;
            padding: 1 2 0 2;
        }
        PromptScreen #prompt-help {
            color: $text-muted;
            margin-bottom: 1;
        }
        PromptScreen Input {
            height: 1;
            border: none;
            padding: 0;
            background: $surface;
            color: $foreground;
        }
        PromptScreen Input:focus {
            border: none;
            background: $surface;
            background-tint: transparent;
        }
        PromptScreen #prompt-error {
            color: $error;
            height: 1;
            margin-top: 1;
        }
    """

    def __init__(
        self,
        title: str,
        help_text: str,
        *,
        placeholder: str = "",
        value: str = "",
        suggester: Suggester | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._help = help_text
        self._placeholder = placeholder
        self._value = value
        self._suggester = suggester
        self._validate = validate

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-panel"):
            yield Static(Text(self._help), id="prompt-help")
            yield Input(
                value=self._value,
                placeholder=self._placeholder,
                suggester=self._suggester,
                id="prompt-input",
            )
            yield Static("", id="prompt-error")

    def on_mount(self) -> None:
        panel = self.query_one("#prompt-panel")
        panel.border_title = self._title
        panel.border_subtitle = "Enter applies · Tab completes · esc cancels"
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Changed)
    def _clear_error(self) -> None:
        self.query_one("#prompt-error", Static).update("")

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if self._validate is not None:
            error = self._validate(text)
            if error:
                self.query_one("#prompt-error", Static).update(Text(error))
                return
        self.dismiss(text)

    def action_complete(self) -> None:
        # Right arrow at the end of the line accepts the greyed suggestion.
        self.query_one("#prompt-input", Input).action_cursor_right()

    def action_cancel(self) -> None:
        self.dismiss(None)
