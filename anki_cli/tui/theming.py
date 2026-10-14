"""Theme plumbing shared by the browse and review apps (#85).

CSS reads the active theme through ``$variables``. Rich ``Text`` styles built in
Python cannot, so they read a :class:`Palette` resolved from the same theme and
are rebuilt when the theme changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from textual.app import App, SystemCommand
from textual.color import Color
from textual.screen import Screen
from textual.theme import Theme

from .colors import TOKYO_NIGHT

# Textual's command palette is its own screen; this restyles it from the theme so
# it matches the apps instead of showing its stock slab (#85).
PALETTE_CSS = """
CommandPalette {
    background: $background 70%;
}
CommandPalette > Vertical {
    width: 80%;
    max-width: 96;
    margin-top: 2;
}
CommandPalette #--input {
    background: $background;
    border: round $primary;
}
CommandPalette #--input.--list-visible {
    border: round $primary;
}
CommandPalette #--input Input {
    background: transparent;
}
CommandPalette SearchIcon {
    color: $primary;
    margin-top: 0;
}
CommandPalette CommandInput,
CommandPalette CommandInput:focus {
    border: none;
    height: 1;
    padding-left: 1;
}
CommandPalette CommandList {
    background: $background;
    border: none;
    scrollbar-size: 1 1;
    scrollbar-color: $border;
    scrollbar-color-hover: $text-muted;
    scrollbar-background: $background;
}
CommandPalette CommandList.--visible,
CommandPalette CommandList.--visible:focus {
    border: round $border;
}
CommandPalette CommandList > .option-list--option {
    color: $foreground;
    text-style: bold;
}
CommandPalette CommandList > .option-list--option-highlighted {
    background: $panel;
    color: $foreground;
    text-style: bold;
}
CommandPalette > .command-palette--help-text {
    color: $text-muted;
}
CommandPalette > .command-palette--highlight {
    color: $primary;
    text-style: bold underline;
}
.-no-color CommandPalette > .command-palette--help-text {
    color: $foreground;
}
"""

# Built-in palette entries that mean nothing in a terminal card reviewer.
_HIDDEN_SYSTEM_COMMANDS = frozenset({"Screenshot", "Maximize", "Minimize"})
# The ones we keep, renamed to say what they do and show their key.
_SYSTEM_COMMAND_TITLES = {"Keys": "Show keys  (?)", "Theme": "Change theme", "Quit": "Quit  (q)"}


def command(
    title: str, key: str | None, callback: Callable[[], Any], *, common: bool = True
) -> SystemCommand:
    """A one-line palette entry: the key sits in the title and there is no
    second line of help. ``common=False`` keeps it out of the empty-search list,
    so the list opens short and the rest is found by typing."""
    return SystemCommand(f"{title}  ({key})" if key else title, "", callback, common)


DEFAULT_THEME = TOKYO_NIGHT.name
# Selected by --no-color / display.color = false: the terminal's own palette.
NO_COLOR_THEME = "textual-ansi"


def _style_color(value: str, fallback: str) -> str:
    """A Rich-safe colour name for a Textual colour string (hex or ``ansi_*``)."""
    try:
        return str(Color.parse(value).rich_color.name)
    except Exception:
        return fallback


@dataclass(frozen=True, slots=True)
class Palette:
    """Rich style colours for the active theme."""

    primary: str
    secondary: str
    accent: str
    success: str
    warning: str
    error: str
    text: str
    muted: str

    @classmethod
    def from_variables(cls, variables: Mapping[str, str]) -> Palette:
        base = TOKYO_NIGHT

        def pick(name: str, default: str) -> str:
            return _style_color(variables.get(name) or default, default)

        text = pick("foreground", str(base.foreground))
        muted = "color(8)" if text in {"default"} or text.startswith("color(") else ""
        if not muted:
            try:
                bg = Color.parse(variables.get("background") or str(base.background))
                fg = Color.parse(variables.get("foreground") or str(base.foreground))
                muted = bg.blend(fg, 0.5).hex
            except Exception:
                muted = "#565f89"
        return cls(
            primary=pick("primary", str(base.primary)),
            secondary=pick("secondary", str(base.secondary)),
            accent=pick("accent", str(base.accent)),
            success=pick("success", str(base.success)),
            warning=pick("warning", str(base.warning)),
            error=pick("error", str(base.error)),
            text=text,
            muted=muted,
        )

    @classmethod
    def from_theme(cls, theme: Theme) -> Palette:
        variables: dict[str, str] = {
            "primary": theme.primary,
            "secondary": theme.secondary or theme.primary,
            "accent": theme.accent or theme.primary,
            "success": theme.success or "",
            "warning": theme.warning or "",
            "error": theme.error or "",
            "foreground": theme.foreground or "",
            "background": theme.background or "",
        }
        return cls.from_variables({k: v for k, v in variables.items() if v})


# What the module-level row/preview formatters use when they are not handed a
# palette (and what the unit tests see): the shipped Tokyo Night look.
DEFAULT_PALETTE = Palette.from_theme(TOKYO_NIGHT)


class ThemedApp(App[None]):
    """Registers the shipped theme, applies the configured one and keeps it."""

    def __init__(
        self,
        *,
        theme: str | None = None,
        no_color: bool = False,
        persist_theme: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(TOKYO_NIGHT)
        self._persist_theme = persist_theme
        self._no_color = no_color
        self._theme_warning: str | None = None

        wanted = NO_COLOR_THEME if no_color else (theme or DEFAULT_THEME)
        if no_color:
            # ansi_* colours need the terminal's palette passed through.
            self.ansi_color = True
            self.add_class("-no-color")
        if wanted not in self.available_themes:
            self._theme_warning = f"Unknown theme '{wanted}'; using {DEFAULT_THEME}."
            wanted = DEFAULT_THEME
        self.theme = wanted

    @property
    def palette(self) -> Palette:
        variables: Mapping[str, Any] = self.theme_variables
        return Palette.from_variables({k: str(v) for k, v in variables.items()})

    def _theme_ready(self) -> None:
        """Call from ``on_mount``: warn about a bad setting, watch for changes."""
        if self._theme_warning:
            self.notify(self._theme_warning, severity="warning", markup=False)
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)

    def _on_theme_changed(self, theme: Theme) -> None:
        # The palette picker should stick (#5), but --no-color is a per-run
        # choice and must not overwrite the saved theme.
        if self._persist_theme is not None and not self._no_color:
            try:
                self._persist_theme(theme.name)
            except Exception as exc:
                self.notify(f"Could not save theme: {exc}", severity="warning", markup=False)
        self.on_palette_changed()
        for screen in self.screen_stack:
            hook = getattr(screen, "on_palette_changed", None)
            if callable(hook):
                hook()

    def app_commands(self) -> Iterable[SystemCommand]:
        """Commands for this screen, listed first in the command palette."""
        return ()

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        # A screen that brings its own commands (the review screen pushed over
        # browse) replaces the app's: the app's act on the screen underneath.
        provider = getattr(screen, "app_commands", None)
        yield from provider() if callable(provider) else self.app_commands()
        for builtin in super().get_system_commands(screen):
            if builtin.title in _HIDDEN_SYSTEM_COMMANDS:
                continue
            title = _SYSTEM_COMMAND_TITLES.get(builtin.title, builtin.title)
            yield builtin._replace(title=title, help="")

    def on_palette_changed(self) -> None:
        """Rebuild any Rich text that baked the old colours in."""
