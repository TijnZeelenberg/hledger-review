from pathlib import Path

from textual.widgets import Checkbox, Input, RadioSet

from hledger_review.app import ReviewApp
from hledger_review.config import Config, load
from hledger_review.journal import Txn, parse_journal

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
        await pilot.press("ctrl+s")
        assert app.changed == 2
        assert app.current is not None and app.current.date == "2026-01-05"
        await pilot.press("ctrl+q")

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
        await pilot.press("ctrl+s")
        assert app.changed == 0
        await pilot.press("ctrl+s")
        assert app.changed == 1
    assert "account expenses:food:snacks" in (workdir / "accounts.journal").read_text()


async def test_learned_rule_is_appended(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    rules_before = (workdir / "bank.csv.rules").read_text()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")  # skip Albert Heijn, on to Spotify AB
        assert app.current is not None and app.current.raw_name == "Spotify AB"
        app.query_one("#desc", Input).value = "Spotify"
        app.query_one("#account", Input).value = "expenses:subscriptions"
        app.query_one("#rule-field", RadioSet).action_next_button()
        app.query_one("#rule-field", RadioSet).action_toggle_button()
        await pilot.pause()
        assert app.query_one("#pattern", Input).value == "Spotify AB"
        await pilot.press("ctrl+s")
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
        await pilot.press("ctrl+s")
        assert app.changed == 0


async def test_launches_with_nothing_to_review(workdir: Path) -> None:
    config = load()
    segments = parse_journal(config.journal.read_text())
    app = ReviewApp(config, segments, [], accounts=list(ACCOUNTS))
    async with app.run_test() as pilot:
        assert app.query_one("#detail").border_title == "Nothing to review"
        assert not app.query_one("#desc", Input).display
        await pilot.press("ctrl+s", "ctrl+n")
        assert app.changed == 0
        await pilot.press("ctrl+q")
    assert app.return_value == 0
