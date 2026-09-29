import contextlib
from pathlib import Path

import pytest
from textual.widgets import Checkbox, DataTable, Input, RadioSet

from hledger_review import hledger
from hledger_review.app import ReviewApp
from hledger_review.config import Config, load
from hledger_review.importer import load_items, year_transactions
from hledger_review.journal import Txn
from hledger_review.widgets import ModalInput
from tests.conftest import needs_hledger

pytestmark = needs_hledger  # every save regenerates with hledger

ACCOUNTS = [
    "assets:checking",
    "expenses:food:groceries",
    "expenses:subscriptions",
    "expenses:unknown",
]
AH_03 = "%date ^20260103$ && %saldo ^976,01$ && %bedrag ^23,99$ && %direction ^Debit$"
AH_06 = "%date ^20260106$ && %saldo ^959,02$ && %bedrag ^5,00$ && %direction ^Debit$"
SPOTIFY = "%date ^20260105$ && %saldo ^964,02$ && %bedrag ^11,99$ && %direction ^Debit$"


def make_app(config: Config) -> ReviewApp:
    return ReviewApp(config, load_items(config, None, False), accounts=list(ACCOUNTS))


def journal_txn(config: Config, date: str) -> Txn:
    txns = year_transactions(config.source(None), 2026)
    return next(t for t in txns if t.date == date)


def one_offs(workdir: Path) -> list[str]:
    text = (workdir / "2026" / "one-offs.rules").read_text()
    return text.split("if|account1|description|comment\n")[1].splitlines()


def files(workdir: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in workdir.rglob("*") if p.is_file()}


def pick_rule_field(app: ReviewApp) -> None:
    radio = app.query_one("#rule-field", RadioSet)
    radio.action_next_button()
    radio.action_toggle_button()


# saving
async def test_save_writes_one_offs_for_the_same_name(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        assert app.query_one("#same", Checkbox).value  # two Albert Heijn 1234
        app.query_one("#desc", Input).value = "Groceries"
        app.query_one("#account", Input).value = "expenses:food:groceries"
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 2
        assert app.current is not None and app.current.txn.date == "2026-01-05"
        table = app.query_one("#list", DataTable)
        assert table.get_cell("bank:2026:2", "name") == "Groceries"
        await pilot.press("q")

    assert one_offs(workdir)[1:] == [
        f"{AH_03}|expenses:food:groceries|Groceries|",
        f"{AH_06}|expenses:food:groceries|Groceries|",
    ]
    for date in ("2026-01-03", "2026-01-06"):
        t = journal_txn(config, date)
        assert (t.description, t.comment) == ("Groceries", "")
        assert t.account(config.roles) == "expenses:food:groceries"
    assert journal_txn(config, "2026-01-03").lines[2].endswith("= €976.01")
    assert journal_txn(config, "2026-01-05").account(config.roles) == "expenses:unknown"


async def test_same_description_keeps_the_comment(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        await pilot.press("n")  # on to Spotify AB
        app.query_one("#account", Input).value = "expenses:subscriptions"
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 1
    assert one_offs(workdir)[1:] == [
        f"{SPOTIFY}|expenses:subscriptions|Spotify AB|Subscription"
    ]
    t = journal_txn(config, "2026-01-05")
    assert (t.description, t.comment) == ("Spotify AB", "Subscription")


def tagged(config: Config, query: str) -> list[str]:
    """Dates of the transactions hledger finds with `tag:QUERY`."""
    out = hledger.run(config.journal, "print", f"tag:{query}").stdout
    return [line.split()[0] for line in out.splitlines() if line[:1].isdigit()]


async def test_tags_go_in_the_one_off_comment(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        app.query_one("#desc", Input).value = "Groceries"  # clears the notes
        app.query_one("#account", Input).value = "expenses:food:groceries"
        app.query_one("#tags", Input).value = " reis : gent| vast"
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 2
        assert app.query_one("#tags", Input).value == ""  # cleared for the next
    assert one_offs(workdir)[1:] == [
        f"{AH_03}|expenses:food:groceries|Groceries|reis:gent, vast:",
        f"{AH_06}|expenses:food:groceries|Groceries|reis:gent, vast:",
    ]
    assert tagged(config, "reis=gent") == ["2026-01-03", "2026-01-06"]
    assert tagged(config, "vast") == ["2026-01-03", "2026-01-06"]


async def test_tags_follow_a_kept_comment(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        await pilot.press("n")  # on to Spotify AB, notes "Subscription"
        app.query_one("#account", Input).value = "expenses:subscriptions"
        app.query_one("#tags", Input).value = "vast"
        await pilot.pause()
        await pilot.press("w")
    assert one_offs(workdir)[1:] == [
        f"{SPOTIFY}|expenses:subscriptions|Spotify AB|Subscription, vast:"
    ]
    t = journal_txn(config, "2026-01-05")
    assert t.comment == "Subscription, vast:"
    assert tagged(config, "vast") == ["2026-01-05"]


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


async def test_learned_rule_is_appended_and_applied(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    rules_before = (workdir / "bank.rules").read_text()
    one_offs_before = (workdir / "2026" / "one-offs.rules").read_text()
    async with app.run_test() as pilot:
        await pilot.press("n")  # skip Albert Heijn, on to Spotify AB
        assert app.current is not None
        assert app.current.txn.description == "Spotify AB"
        app.query_one("#desc", Input).value = "Spotify"
        app.query_one("#account", Input).value = "expenses:subscriptions"
        pick_rule_field(app)
        await pilot.pause()
        assert app.query_one("#pattern", Input).value == "Spotify AB"
        await pilot.press("w")
        assert app.changed == 1
    assert (workdir / "bank.rules").read_text() == rules_before + (
        "\nif %payee Spotify AB\n"
        "  account1     expenses:subscriptions\n"
        "  description  Spotify\n"
        "  comment\n"
    )
    assert (workdir / "2026" / "one-offs.rules").read_text() == one_offs_before
    t = journal_txn(config, "2026-01-05")
    assert t.description == "Spotify"
    assert t.account(config.roles) == "expenses:subscriptions"


async def test_tags_go_in_the_rule_comment(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    rules_before = (workdir / "bank.rules").read_text()
    async with app.run_test() as pilot:
        await pilot.press("n")
        app.query_one("#account", Input).value = "expenses:subscriptions"
        app.query_one("#tags", Input).value = "vast, muziek"
        pick_rule_field(app)
        await pilot.pause()
        await pilot.press("w")
        assert app.changed == 1
    assert (workdir / "bank.rules").read_text() == rules_before + (
        "\nif %payee Spotify AB\n"
        "  account1     expenses:subscriptions\n"
        "  description  Spotify AB\n"
        "  comment      vast:, muziek:\n"
    )
    assert tagged(config, "muziek") == ["2026-01-05"]


async def test_bad_tag_blocks_save(workdir: Path) -> None:
    config = load()
    before = files(workdir)
    app = make_app(config)
    async with app.run_test() as pilot:
        app.query_one("#tags", Input).value = "city trip:gent"
        await pilot.press("w")
        assert app.changed == 0
        assert "one word" in str(app.query_one("#rule-error").render())
    assert files(workdir) == before


@pytest.mark.parametrize(
    ("desc", "pattern", "error"),
    [
        ("Groceries", "(unclosed", "invalid regex"),
        ("Groceries", "Jumbo", "does not match %payee"),
        ("Groceries | AH", "Albert", "cannot go in a rules file"),
    ],
)
async def test_bad_input_blocks_save(
    workdir: Path, desc: str, pattern: str, error: str
) -> None:
    config = load()
    before = files(workdir)
    app = make_app(config)
    async with app.run_test() as pilot:
        app.query_one("#desc", Input).value = desc
        pick_rule_field(app)
        await pilot.pause()
        app.query_one("#pattern", Input).value = pattern
        await pilot.press("w")
        assert app.changed == 0
        assert error in str(app.query_one("#rule-error").render())
    assert files(workdir) == before


async def test_launches_with_nothing_to_review(workdir: Path) -> None:
    config = load()
    app = ReviewApp(config, [], accounts=list(ACCOUNTS))
    async with app.run_test() as pilot:
        assert app.query_one("#detail").border_title == "Nothing to review"
        assert not app.query_one("#desc", Input).display
        await pilot.press("w", "n")
        assert app.changed == 0
        await pilot.press("q")
    assert app.return_value == 0


# keys
async def test_j_and_k_move_through_the_list(workdir: Path) -> None:
    config = load()
    app = make_app(config)
    async with app.run_test() as pilot:
        await pilot.press("j", "j")
        assert app.current is not None and app.current.txn.date == "2026-01-06"
        await pilot.press("k")
        assert app.current is not None and app.current.txn.date == "2026-01-05"


async def test_keys_are_modal(workdir: Path) -> None:
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
        await pilot.press("j")
        assert app.focused is app.query_one("#tags", ModalInput)
        await pilot.press("k")
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
        assert app.current is not None and app.current.txn.comment == "Paid back"
        await pilot.press("g")
        assert app.current is not None and app.current.txn.date == "2026-01-03"


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


async def test_e_and_shift_e_edit_and_regenerate(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    rules = workdir / "bank.rules"

    def editor(command: list[str]) -> None:
        calls.append(command)
        if command[-1] == str(rules):  # the user adds a rule
            extra = "\nif %payee Spotify\n  account1  expenses:subscriptions\n"
            rules.write_text(rules.read_text() + extra)

    monkeypatch.setenv("EDITOR", "nvim --clean")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("hledger_review.app.run_editor", editor)
    config = load()
    app = make_app(config)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)  # headless test
    async with app.run_test() as pilot:
        await pilot.press("e")
        assert app.changed == 1  # Spotify, categorised by the new rule
        assert app.status == {"bank:2026:1": "done"}
        await pilot.press("l", "E")  # a field in normal mode: still a command
    one_offs = str(workdir / "2026" / "one-offs.rules")
    assert calls == [["nvim", "--clean", str(rules)], ["nvim", "--clean", one_offs]]
    t = journal_txn(config, "2026-01-05")
    assert t.account(config.roles) == "expenses:subscriptions"
