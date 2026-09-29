from pathlib import Path

from hledger_review.journal import (
    Roles,
    Txn,
    declare_account,
    parse_journal,
    transactions,
)
from tests.conftest import FIXTURES

ROLES = Roles("expenses:unknown", frozenset({"assets:checking"}))


def by_date(date: str) -> Txn:
    text = (FIXTURES / "sample.journal").read_text()
    return next(t for t in transactions(text) if t.date == date)


# parsing
def test_non_transaction_lines_are_kept() -> None:
    text = (FIXTURES / "sample.journal").read_text()
    segments = parse_journal(text)
    assert "include accounts.journal" in segments
    assert [t.date for t in transactions(text)][:2] == ["2026-01-01", "2026-01-03"]


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


def test_balance_assertion() -> None:
    t = by_date("2026-01-03")
    p = t.asset_posting(ROLES)
    assert p is not None and (p.amount, p.assertion) == ("€-23.99", "€976.01")
    assert next(t.postings()).assertion == ""


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
