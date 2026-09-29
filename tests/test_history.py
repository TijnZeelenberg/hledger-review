from pathlib import Path

from hledger_review.config import load
from hledger_review.history import (
    Booking,
    History,
    comment_tags,
    format_tags,
    suggest,
)
from hledger_review.importer import Shared, locate, locate_all, year_transactions
from tests.conftest import needs_hledger


def make(date: str, desc: str, account: str = "expenses:x", tags: str = "") -> Booking:
    return Booking(f"b:{date}", date, "€-1.00", None, account, desc, tags, "P", True)


def test_comment_tags_skip_bank_text() -> None:
    comment = "Omschrijving: boodschappen, reis:gent, vast:, city:new york"
    tags = comment_tags(comment)
    assert tags == [("reis", "gent"), ("vast", ""), ("city", "new york")]
    assert format_tags(tags) == "reis:gent, vast, city:new york"
    assert comment_tags("a:1\nb:") == [("a", "1"), ("b", "")]


def test_locate_all_matches_locate(workdir: Path) -> None:
    source = load().source(None)
    shared = Shared(source)
    txns = year_transactions(source, 2026)
    assert locate_all(shared, 2026, txns) == [
        locate(shared, 2026, txns, i) for i in range(len(txns))
    ]


def test_history_by_bank_name(history_workdir: Path) -> None:
    history = History(load())
    found, by_payee = history.earlier("bank:2026:0")  # Albert Heijn 1234
    assert by_payee
    assert [(b.date, b.description, b.tags) for b in found] == [
        ("2025-06-01", "Groceries", "reis:gent"),
        ("2025-03-01", "Snacks", ""),
        ("2025-02-01", "Groceries", ""),
        ("2025-01-05", "Groceries", ""),
    ]
    assert {b.payee for b in found} == {"Albert Heijn 1234"}
    assert found[0].amount == "€-8.00"


def test_history_falls_back_to_the_amount(history_workdir: Path) -> None:
    history = History(load())
    found, by_payee = history.earlier("bank:2026:1")  # Spotify AB, €-11.99
    assert not by_payee
    assert [(b.payee, b.amount, b.tags) for b in found] == [
        ("Cafe Praag", "€-12.00", "reis:praag"),  # a cent off still counts
        ("Spotify P123", "€-11.99", "vast"),
    ]
    assert history.suggest("bank:2026:1") is None  # never from the amount
    assert history.earlier("bank:2026:4") == ([], False)  # A Friend: nothing


@needs_hledger
def test_history_refreshes_a_year(history_workdir: Path) -> None:
    config = load()
    history = History(config)
    source = config.source(None)
    journal = source.output(2026)
    journal.write_text(
        journal.read_text().replace(
            "2026-01-03 Albert Heijn 1234  ; Omschrijving: boodschappen pas 12\n"
            "    expenses:unknown",
            "2026-01-03 Weekly shop\n    expenses:food:groceries",
        )
    )
    history.refresh(source, 2026)
    found, _ = history.earlier("bank:2026:2")
    assert (found[0].date, found[0].description) == ("2026-01-03", "Weekly shop")


def test_suggest_picks_the_most_frequent(history_workdir: Path) -> None:
    s = History(load()).suggest("bank:2026:0")
    assert s is not None
    assert (s.booking.description, s.booking.account, s.booking.tags) == (
        "Groceries",
        "expenses:food:groceries",
        "",
    )
    assert (s.count, s.total) == (2, 4)
    assert s.hint == "suggested from 2 of 4 earlier"


def test_suggest_breaks_ties_by_date() -> None:
    s = suggest(
        [
            make("2025-02-01", "Cake"),
            make("2025-03-01", "Bread"),
            make("2025-01-01", "Cake"),
            make("2025-01-02", "Bread"),
        ]
    )
    assert s is not None and s.booking.description == "Bread"
    assert suggest([make("2025-01-01", "Tea")]) is not None
    assert suggest([]) is None
    one = suggest([make("2025-01-01", "Tea"), make("2025-01-02", "Tea")])
    assert one is not None and one.hint == "suggested from 2 earlier"
