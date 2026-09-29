"""Follow the active Omarchy theme: read its colors.toml into a Textual theme.

Omarchy generates every app's colours from `colors.toml` in the current theme
directory. Anything missing or unreadable means "not Omarchy": the app then
keeps a built-in theme.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rich.terminal_theme import TerminalTheme
from textual.color import Color, ColorParseError
from textual.theme import Theme

FALLBACK = "tokyo-night"
THEME_DIRS = (
    Path.home() / ".local/state/omarchy/current/theme",
    Path.home() / ".config/omarchy/current/theme",
)
ANSI = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")


@dataclass(frozen=True)
class Palette:
    """The colours of an Omarchy theme, as #rrggbb strings."""

    dark: bool
    background: str
    foreground: str
    accent: str
    muted: str
    selection: str
    surface: str
    normal: tuple[str, ...]
    bright: tuple[str, ...]

    def ansi(self, name: str) -> str:
        return self.normal[ANSI.index(name)]


def theme_dir() -> Path | None:
    """$HLEDGER_REVIEW_THEME_DIR, else the active Omarchy theme, if any."""
    if env := os.environ.get("HLEDGER_REVIEW_THEME_DIR"):
        return Path(env)
    return next((d for d in THEME_DIRS if d.is_dir()), None)


def load_palette(directory: Path | None) -> Palette | None:
    """Parse DIRECTORY/colors.toml; None when it is missing or invalid."""
    if directory is None:
        return None
    try:
        raw = tomllib.loads((directory / "colors.toml").read_text())
        return parse_palette(raw)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def parse_palette(raw: dict[str, object]) -> Palette | None:
    """Build a palette from either colors.toml schema (named or color0..15)."""

    def get(*keys: str) -> Color | None:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str):
                return Color.parse(value)
        return None

    try:
        bg = get("background", "bg")
        fg = get("foreground", "fg")
        if bg is None or fg is None:
            return None
        normal = [get(name, f"color{i}") for i, name in enumerate(ANSI)]
        if any(c is None for c in normal[1:7]):
            return None
        bright = [get(f"bright_{name}", f"color{i + 8}") for i, name in enumerate(ANSI)]
        muted = get("muted", "color8") or bg.blend(fg, 0.45)
        # ANSI black/white/bright black may be absent in the named schema
        normal[0] = normal[0] or bg
        normal[7] = normal[7] or fg
        bright[0] = bright[0] or muted
        bright[7] = bright[7] or get("bright_foreground", "bright_fg") or fg
        full = [n if b is None else b for n, b in zip(normal, bright, strict=True)]
        mode = raw.get("mode")
        return Palette(
            dark=mode == "dark" if mode in ("dark", "light") else bg.brightness < 0.5,
            background=bg.hex,
            foreground=fg.hex,
            accent=(get("accent") or normal[4] or fg).hex,
            muted=muted.hex,
            selection=(get("selection") or bg.blend(fg, 0.15)).hex,
            surface=(get("lighter_background", "lighter_bg") or bg.blend(fg, 0.06)).hex,
            normal=tuple(c.hex for c in normal if c is not None),
            bright=tuple(c.hex for c in full if c is not None),
        )
    except ColorParseError:
        return None


def textual_theme(p: Palette) -> Theme:
    """Map the palette onto Textual's theme variables."""
    bg = Color.parse(p.background)
    selection = Color.parse(p.selection)
    return Theme(
        name="omarchy",
        dark=p.dark,
        primary=p.accent,
        secondary=p.ansi("blue"),
        accent=p.accent,
        warning=p.ansi("yellow"),
        error=p.ansi("red"),
        success=p.ansi("green"),
        foreground=p.foreground,
        background=p.background,
        surface=p.background,
        panel=p.surface,
        variables={
            "border": p.accent,
            "border-blurred": p.muted,
            "block-cursor-background": p.selection,
            "block-cursor-foreground": p.foreground,
            "block-cursor-text-style": "none",
            "block-cursor-blurred-background": bg.blend(selection, 0.6).hex,
            "block-cursor-blurred-foreground": p.foreground,
            "block-cursor-blurred-text-style": "none",
            "block-hover-background": bg.blend(selection, 0.4).hex,
            "input-selection-background": p.selection,
            "input-cursor-background": p.foreground,
            "input-cursor-foreground": p.background,
            "footer-background": p.background,
            "footer-key-foreground": p.accent,
            "footer-description-foreground": p.foreground,
            "scrollbar": p.muted,
            "scrollbar-hover": p.accent,
            "scrollbar-active": p.accent,
            "scrollbar-background": p.background,
        },
    )


def terminal_theme(p: Palette) -> TerminalTheme:
    """The palette as ANSI colours, so Rich's red/green/... follow the theme."""

    def rgb(value: str) -> tuple[int, int, int]:
        return Color.parse(value).rgb

    return TerminalTheme(
        rgb(p.background),
        rgb(p.foreground),
        [rgb(c) for c in p.normal],
        [rgb(c) for c in p.bright],
    )
