"""Theme plumbing shared by the browse and review apps (#85).

CSS reads the active theme through ``$variables``. Rich ``Text`` styles built in
Python cannot, so they read a :class:`Palette` resolved from the same theme and
are rebuilt when the theme changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from textual.app import App
from textual.color import Color
from textual.theme import Theme

from .colors import TOKYO_NIGHT

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

    def on_palette_changed(self) -> None:
        """Rebuild any Rich text that baked the old colours in."""
