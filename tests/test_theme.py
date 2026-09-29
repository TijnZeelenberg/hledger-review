from pathlib import Path

import pytest

from hledger_review import theme
from hledger_review.app import ReviewApp
from hledger_review.config import load

NAMED = """\
mode = "light"
accent = "#1e66f5"
muted = "#acb0be"
selection = "#ccd0da"
background = "#eff1f5"
foreground = "#4c4f69"
red = "#d20f39"
green = "#40a02b"
yellow = "#df8e1d"
blue = "#1e66f5"
magenta = "#ea76cb"
cyan = "#179299"
"""

ANSI_ONLY = """\
background = "#1c1e26"
foreground = "#cbced0"
""" + "\n".join(
    f'color{i} = "#{c}"'
    for i, c in enumerate(
        ["1c1e26", "e95678", "29d398", "fac29a", "26bbd9", "ee64ac", "59e1e3", "cbced0"]
        + ["6f6f70"] * 8
    )
)


def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "colors.toml").write_text(text)
    return tmp_path


def test_named_palette(tmp_path: Path) -> None:
    p = theme.load_palette(write(tmp_path, NAMED))
    assert p is not None
    assert not p.dark
    assert (p.background, p.accent, p.muted) == ("#EFF1F5", "#1E66F5", "#ACB0BE")
    assert (p.ansi("red"), p.ansi("green")) == ("#D20F39", "#40A02B")
    # no color0/7: ANSI black and white fall back to background and foreground
    assert (p.ansi("black"), p.ansi("white")) == ("#EFF1F5", "#4C4F69")
    assert len(p.normal) == len(p.bright) == 8


def test_ansi_only_palette(tmp_path: Path) -> None:
    p = theme.load_palette(write(tmp_path, ANSI_ONLY))
    assert p is not None
    assert p.dark  # no mode: judged by the background
    assert p.accent == "#26BBD9"  # no accent: ANSI blue
    assert (p.ansi("red"), p.muted) == ("#E95678", "#6F6F70")


def test_textual_theme_maps_the_palette(tmp_path: Path) -> None:
    p = theme.load_palette(write(tmp_path, NAMED))
    assert p is not None
    t = theme.textual_theme(p)
    assert (t.primary, t.error, t.success, t.background) == (
        "#1E66F5",
        "#D20F39",
        "#40A02B",
        "#EFF1F5",
    )
    assert t.variables["border"] == p.accent
    assert t.variables["border-blurred"] == p.muted
    assert not t.dark
    assert theme.terminal_theme(p).ansi_colors[1] == (210, 15, 57)


@pytest.mark.parametrize(
    "text",
    [
        "background = ",  # not TOML
        NAMED.replace("#d20f39", "not-a-colour"),
        NAMED.replace('background = "#eff1f5"', ""),
        NAMED.replace('red = "#d20f39"', ""),
    ],
)
def test_bad_files_give_no_palette(tmp_path: Path, text: str) -> None:
    assert theme.load_palette(write(tmp_path, text)) is None


def test_missing_dir_gives_no_palette(tmp_path: Path) -> None:
    assert theme.load_palette(tmp_path / "nope") is None
    assert theme.load_palette(None) is None


def test_theme_dir_follows_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLEDGER_REVIEW_THEME_DIR", str(tmp_path))
    assert theme.theme_dir() == tmp_path


async def test_app_uses_the_omarchy_theme(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLEDGER_REVIEW_THEME_DIR", str(write(workdir, NAMED)))
    config = load(None)
    app = ReviewApp(config, [], accounts=[])
    async with app.run_test():
        assert app.theme == "omarchy"
        assert app.ansi_theme.ansi_colors[1] == (210, 15, 57)


async def test_app_falls_back_without_a_theme(workdir: Path) -> None:
    config = load(None)
    app = ReviewApp(config, [], accounts=[])
    async with app.run_test():
        assert app.theme == theme.FALLBACK
