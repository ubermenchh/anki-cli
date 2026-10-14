from textual.theme import Theme

BLUE = "#7aa2f7"
CYAN = "#7dcfff"
GREEN = "#9ece6a"
RED = "#f7768e"
YELLOW = "#e0af68"
DIM = "#565f89"
BORDER = "#3b4261"
TOOLBAR_BG = "#16161e"
MENU_BG = "#1f2335"
SELECTION_BG = "#283457"
TEXT = FG = "#c0caf5"

# Registered on the apps so Textual's own widgets (Footer, Tabs, HelpPanel,
# toasts, scrollbars) pick up the same palette as the hand-styled ones.
TOKYO_NIGHT = Theme(
    name="anki-tokyo-night",
    primary=BLUE,
    secondary="#bb9af7",
    accent=CYAN,
    warning=YELLOW,
    error=RED,
    success=GREEN,
    foreground=FG,
    background=TOOLBAR_BG,
    surface=MENU_BG,
    panel=SELECTION_BG,
    dark=True,
    variables={
        "block-cursor-background": SELECTION_BG,
        "block-cursor-foreground": FG,
        "border": BORDER,
        "border-blurred": BORDER,
        "button-color-foreground": TOOLBAR_BG,
    },
)
