import contextlib
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input, RadioSet

from hledger_review.app import ReviewApp
from hledger_review.config import Config, load
from hledger_review.journal import Txn, parse_journal
from hledger_review.widgets import ModalInput

ACCOUNTS = [
    "assets:checking",
    "expenses:food:groceries",
    "expenses:subscriptions",
    "expenses:unknown",
]


def make_app(config: Config) -> ReviewApp:
    segments = parse_journal(config.journal.read_text())
    todo = [
        s for s in segments if isinstance(s, Txn) and config.unmarked in s.accounts()
    ]
    return ReviewApp(config, segments, todo, accounts=list(ACCOUNTS))


def journal_txn(config: Config, date: str) -> Txn:
    segments = parse_journal(config.journal.read_text())
    return next(s for s in segments if isinstance(s, Txn) and s.date == date)


async def test_save_rewrites_in_place_and_applies_to_same_name(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        assert app.query_one("#same", Checkbox).value  # two Albert Heijn 1234
        app.query_one("#desc", Input).value = "Groceries"
        app.query_one("#account", Input).value = "expenses:food:groceries"
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 2
        assert app.current is not None and app.current.date == "2026-01-05"
        await pilot.press("q")

    for date in ("2026-01-03", "2026-01-06"):
        t = journal_txn(config, date)
        assert t.description == "Groceries"
        assert t.comment == ""
        assert t.account(config.roles) == "expenses:food:groceries"
    assert journal_txn(config, "2026-01-03").lines[2].endswith("= €976.01")
    assert journal_txn(config, "2026-01-05").account(config.roles) == "expenses:unknown"


async def test_new_account_needs_confirmation_and_is_declared(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        app.query_one("#same", Checkbox).value = False
        app.query_one("#account", Input).value = "expenses:food:snacks"
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 0
        await pilot.press("w")
        assert app.changed == 1
    assert "account expenses:food:snacks" in (workdir / "accounts.journal").read_text()


async def test_learned_rule_is_appended(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    rules_before = (workdir / "bank.csv.rules").read_text()
    async with app.run_test() as pilot:
        await pilot.press("n")  # skip Albert Heijn, on to Spotify AB
        assert app.current is not None and app.current.raw_name == "Spotify AB"
        app.query_one("#desc", Input).value = "Spotify"
        app.query_one("#account", Input).value = "expenses:subscriptions"
        app.query_one("#rule-field", RadioSet).action_next_button()
        app.query_one("#rule-field", RadioSet).action_toggle_button()
        await pilot.pause()
        assert app.query_one("#pattern", Input).value == "Spotify AB"
        await pilot.press("w")
    assert (workdir / "bank.csv.rules").read_text() == rules_before + (
        "\nif %payee Spotify AB\n"
        "  account2     expenses:subscriptions\n"
        "  description  Spotify\n"
        "  comment\n"
    )


async def test_invalid_rule_pattern_blocks_save(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        radio = app.query_one("#rule-field", RadioSet)
        radio.action_next_button()
        radio.action_toggle_button()
        await pilot.pause()
        app.query_one("#pattern", Input).value = "(unclosed"
        await pilot.press("w")
        assert app.changed == 0


async def test_launches_with_nothing_to_review(workdir: Path) -> None:
    config = load()
    segments = parse_journal(config.journal.read_text())
    app = ReviewApp(config, segments, [], accounts=list(ACCOUNTS))
    async with app.run_test() as pilot:
        assert app.query_one("#detail").border_title == "Nothing to review"
        assert not app.query_one("#desc", Input).display
        await pilot.press("w", "n")
        assert app.changed == 0
        await pilot.press("q")
    assert app.return_value == 0


async def test_j_and_k_move_through_the_list(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        await pilot.press("j", "j")
        assert app.current is not None and app.current.date == "2026-01-06"
        await pilot.press("k")
        assert app.current is not None and app.current.date == "2026-01-05"


async def test_keys_are_modal(workdir: Path) -> None:
    from textual.widgets import DataTable

    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        desc = app.query_one("#desc", ModalInput)
        await pilot.press("l")  # list -> form, still normal mode
        assert app.focused is desc and not desc.editing
        before = desc.value
        await pilot.press("x", "backspace")  # normal mode: not text
        assert desc.value == before
        await pilot.press("j")  # next field
        assert app.focused is app.query_one("#account", ModalInput)
        await pilot.press("k", "i")  # back up, insert
        assert app.focused is desc and desc.editing
        assert app.sub_title.endswith("-- INSERT --")
        desc.value = ""
        await pilot.press("w", "n", "q", "h", "j")  # insert mode: all text
        assert desc.value == "wnqhj" and app.changed == 0 and app.is_running
        await pilot.press("escape")  # normal again, still on the field
        assert app.focused is desc and not desc.editing
        await pilot.press("h")
        assert isinstance(app.focused, DataTable)
        await pilot.press("G")
        assert app.current is not None and app.current.date == "2026-01-08"
        await pilot.press("g")
        assert app.current is not None and app.current.date == "2026-01-03"


async def test_leaving_a_field_ends_insert_mode(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        await pilot.press("l", "i", "enter")  # Enter in the description
        account = app.query_one("#account", ModalInput)
        assert app.focused is account and not account.editing
        assert not app.query_one("#desc", ModalInput).editing


async def test_enter_saves_and_stays_in_the_form(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        app.query_one("#same", Checkbox).value = False
        app.query_one("#desc", ModalInput).value = "Groceries"
        app.query_one("#account", ModalInput).value = "expenses:food:groceries"
        await pilot.pause()
        await pilot.press("l", "j", "i", "enter")  # insert on account, Enter
        assert app.changed == 1
        desc = app.query_one("#desc", ModalInput)
        assert app.focused is desc and not desc.editing


async def test_e_opens_the_rules_file(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("EDITOR", "nvim --clean")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("hledger_review.app.run_editor", calls.append)
    config = load()
    app = make_app(config)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)  # headless test
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.press("l", "e")  # a field in normal mode: still a command
    rules = str(workdir / "bank.csv.rules")
    assert calls == [["nvim", "--clean", rules], ["nvim", "--clean", rules]]
