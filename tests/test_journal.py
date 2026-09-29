from pathlib import Path

from hledger_review.journal import (
    Roles,
    Txn,
    declare_account,
    parse_journal,
    render,
)
from tests.conftest import FIXTURES

ROLES = Roles("expenses:unknown", frozenset({"assets:checking"}))


def txns(text: str) -> list[Txn]:
    return [s for s in parse_journal(text) if isinstance(s, Txn)]


def by_date(date: str) -> Txn:
    text = (FIXTURES / "sample.journal").read_text()
    return next(t for t in txns(text) if t.date == date)


# round trip
def test_render_is_lossless() -> None:
    text = (FIXTURES / "sample.journal").read_text()
    assert render(parse_journal(text)) == text


def test_render_is_lossless_without_trailing_newline() -> None:
    text = "2026-01-01 A\n    a  €1\n    b\n\n; end"
    assert render(parse_journal(text)) == text


# parsing
def test_head_fields() -> None:
    t = by_date("2026-01-03")
    assert t.description == "Albert Heijn 1234"
    assert t.comment == "Omschrijving: boodschappen pas 12"
    assert t.amount(ROLES) == "€-23.99"
    assert t.account(ROLES) == "expenses:unknown"


def test_secondary_date_and_status() -> None:
    t = by_date("2026-01-09")
    assert t.date == "2026-01-09"
    assert t.description == "Cash withdrawal"
    assert t.account(ROLES) == "expenses:cash"


def test_unmarked_posting_without_amount() -> None:
    t = by_date("2026-01-08")
    assert t.account(ROLES) == "expenses:unknown"
    assert t.amount(ROLES) == "€20.00"


def test_amount_without_known_asset_account() -> None:
    t = by_date("2026-01-03")
    assert t.amount(Roles("expenses:unknown", frozenset())) == "€23.99"


# editing
def test_set_account_keeps_amount_column() -> None:
    t = by_date("2026-01-03")
    before = t.lines[1].index("€")
    t.set_account("expenses:food:groceries", ROLES)
    assert t.lines[1].strip().startswith("expenses:food:groceries")
    assert t.lines[1].index("€") == before
    assert t.lines[2].endswith("= €976.01")


def test_set_account_longer_than_column_keeps_two_spaces() -> None:
    t = by_date("2026-01-03")
    t.set_account("expenses:a:very:long:account:name:indeed", ROLES)
    assert "indeed  €23.99" in t.lines[1]


def test_set_account_with_tab_separator() -> None:
    t = by_date("2026-01-06")
    t.set_account("expenses:food:groceries", ROLES)
    assert t.lines[1] == "    expenses:food:groceries  €5.00"


def test_set_account_without_amount() -> None:
    t = by_date("2026-01-08")
    t.set_account("expenses:food", ROLES)
    assert t.lines[2] == "    expenses:food"


def test_set_description_drops_or_keeps_comment() -> None:
    t = by_date("2026-01-03")
    t.set_description("Groceries", keep_comment=False)
    assert t.lines[0] == "2026-01-03 * (INV-1) Groceries"
    t = by_date("2026-01-03")
    t.set_description("Albert Heijn 1234", keep_comment=True)
    assert t.lines[0].endswith("Albert Heijn 1234  ; Omschrijving: boodschappen pas 12")


def test_edit_touches_only_that_transaction() -> None:
    text = (FIXTURES / "sample.journal").read_text()
    segments = parse_journal(text)
    t = next(s for s in segments if isinstance(s, Txn) and s.date == "2026-01-05")
    t.set_description("Spotify", keep_comment=False)
    t.set_account("expenses:subscriptions", ROLES)
    out = render(segments)
    diff = [
        (a, b) for a, b in zip(text.split("\n"), out.split("\n"), strict=True) if a != b
    ]
    assert diff == [
        ("2026-01-05 Spotify AB", "2026-01-05 Spotify"),
        (
            "    expenses:unknown                   €11.99",
            "    expenses:subscriptions             €11.99",
        ),
    ]


# accounts file
def test_declare_account_sorts_plain_directives(tmp_path: Path) -> None:
    path = tmp_path / "accounts.journal"
    path.write_text((FIXTURES / "accounts.journal").read_text())
    declare_account(path, "expenses:books")
    lines = path.read_text().split("\n")
    assert lines[:4] == [
        "; account declarations",
        "account assets  ; type: A",
        "account expenses  ; type: X",
        "",
    ]
    plain = [line for line in lines[4:] if line]
    assert plain == sorted(plain)
    assert "account expenses:books" in plain


def test_declare_account_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "new.journal"
    declare_account(path, "expenses:books")
    assert path.read_text() == "account expenses:books\n"
