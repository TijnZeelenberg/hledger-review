from pathlib import Path

import pytest

from hledger_review import hledger
from hledger_review.config import load
from hledger_review.importer import (
    Shared,
    Table,
    locate,
    merge,
    run_generate,
    run_import,
    split_records,
    year_transactions,
)
from tests.conftest import FIXTURES, needs_hledger

DATA = FIXTURES / "2026" / "2026.csv"
JOURNAL = FIXTURES / "2026" / "2026.journal"
OLD = '"20251230";"Bakery";"Bread";"Debit";"3,00";"1000,00"\r\n'


def lines() -> list[str]:
    """The fixture CSV's lines, CRLF kept: the header, then newest first."""
    return DATA.read_bytes().decode().splitlines(keepends=True)


def records() -> tuple[str, list[str]]:
    header, *rows = (r.rstrip("\r\n") for r in lines())
    return header, rows


def export(workdir: Path, rows: list[str]) -> Path:
    """A bank export outside the data folder, as a user would pass it."""
    path = workdir / "Downloads" / "export.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(rows), newline="")
    return path


def data_file(workdir: Path, rows: list[str]) -> None:
    (workdir / "2026" / "2026.csv").write_text("".join(rows), newline="")


def snapshot(workdir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(workdir)): p.read_bytes()
        for p in sorted(workdir.rglob("*"))
        if p.is_file() and "Downloads" not in p.parts
    }


# csv records
def test_split_records_keeps_quoted_newlines() -> None:
    text = 'h;h\r\n"1";"multi\r\nline"\r\n\r\n"2";"x"\r\n'
    assert split_records(text) == ["h;h", '"1";"multi\r\nline"', '"2";"x"']


def test_merge_places_new_rows_where_the_export_has_them(workdir: Path) -> None:
    shared = Shared(load().source(None))
    header, rows = records()
    merged, new = merge(shared, Table([header], rows[:2] + rows[4:]), rows[1:5])
    assert new == rows[2:4]
    assert merged.rows == rows


def test_merge_counts_identical_keys(workdir: Path) -> None:
    shared = Shared(load().source(None))
    header, rows = records()
    table = Table([header], rows)
    assert merge(shared, table, rows) == (table, [])
    merged, new = merge(shared, table, [rows[0], rows[0]])
    assert new == [rows[0]]
    assert merged.rows == [rows[0], *rows]


def test_merge_without_overlap_sorts_by_date(workdir: Path) -> None:
    shared = Shared(load().source(None))
    header, rows = records()
    merged, new = merge(shared, Table([header], rows[3:]), rows[:3])
    assert new == rows[:3]
    assert merged.rows == rows


# generate
@needs_hledger
def test_generate_reproduces_the_journal(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = workdir / "2026" / "2026.journal"
    out.write_text("stale\n")
    assert run_generate(load(), load().sources.values(), []) == 0
    assert out.read_bytes() == JOURNAL.read_bytes()
    assert capsys.readouterr().out == "generated 2026/2026.journal\n"


@needs_hledger
def test_generate_rolls_back_a_failing_check(workdir: Path) -> None:
    data_file(workdir, lines()[:3] + lines()[4:])  # a missing row: assertions fail
    assert run_generate(load(), load().sources.values(), [2026]) == 1
    assert (workdir / "2026" / "2026.journal").read_bytes() == JOURNAL.read_bytes()


# import
@needs_hledger
def test_import_is_idempotent(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load()
    before = snapshot(workdir)
    for rows in (lines(), lines()[:1] + lines()[2:5]):  # the same, and a part
        path = export(workdir, rows)
        assert run_import(config, path, config.source(None), assume_yes=True) == 0
        assert "no new transactions" in capsys.readouterr().out
        assert snapshot(workdir) == before


@needs_hledger
@pytest.mark.parametrize("kept", [slice(3, None), slice(None, -2)])
def test_import_adds_the_newest_or_oldest_rows(
    workdir: Path, capsys: pytest.CaptureFixture[str], kept: slice
) -> None:
    config = load()
    header, *rows = lines()
    data_file(workdir, [header, *rows[kept]])
    assert run_import(config, export(workdir, lines()), config.source(None), True) == 0
    assert (workdir / "2026" / "2026.csv").read_bytes() == DATA.read_bytes()
    assert (workdir / "2026" / "2026.journal").read_bytes() == JOURNAL.read_bytes()
    assert hledger.check(config.journal)[0]
    assert "new row(s) for 2026/2026.csv" in capsys.readouterr().out


@needs_hledger
def test_import_fills_a_gap_from_an_oldest_first_export(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load()
    header, *rows = lines()
    data_file(workdir, [header, *rows[:2], *rows[4:]])
    path = export(workdir, [header, *reversed(rows)])
    assert run_import(config, path, config.source(None), True) == 0
    assert (workdir / "2026" / "2026.csv").read_bytes() == DATA.read_bytes()
    out = capsys.readouterr().out
    assert "2026: 2 new row(s) for 2026/2026.csv, 1 on expenses:unknown" in out


@needs_hledger
def test_import_splits_years_and_starts_a_new_one(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load()
    path = export(workdir, [*lines(), OLD])
    assert run_import(config, path, config.source(None), True) == 0
    assert (workdir / "2026" / "2026.csv").read_bytes() == DATA.read_bytes()
    assert (workdir / "2025" / "2025.csv").read_bytes().decode() == lines()[0] + OLD
    one_offs = (workdir / "2025" / "one-offs.rules").read_text()
    assert one_offs.endswith("\ninclude ../bank.rules\n")
    journal = (workdir / "2025" / "2025.journal").read_text()
    assert journal.startswith("; GENERATED, do not edit: bank 2025 from 2025.csv")
    assert "2025-12-30 Bakery  ; Bread" in journal
    out = capsys.readouterr().out
    assert "2025: 1 new row(s) for 2025/2025.csv (new year), 1 on" in out
    assert "main.journal does not include" in out


@needs_hledger
def test_failed_check_rolls_back_everything(workdir: Path) -> None:
    config = load()
    header, *rows = lines()
    data_file(workdir, [header, *rows[1:]])
    before = snapshot(workdir)
    wrong = rows[0].replace("2909,02", "2909,03")
    path = export(workdir, [header, wrong, *rows[1:], OLD])
    assert run_import(config, path, config.source(None), True) == 1
    assert snapshot(workdir) == before
    assert not (workdir / "2025").exists()


@needs_hledger
def test_declined_prompt_writes_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load()
    data_file(workdir, lines()[:1] + lines()[2:])
    before = snapshot(workdir)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run_import(config, export(workdir, lines()), config.source(None), False) == 1
    assert snapshot(workdir) == before


# mapping transactions to rows
def test_locate_finds_every_row(workdir: Path) -> None:
    source = load().source(None)
    shared = Shared(source)
    txns = year_transactions(source, 2026)
    found = [locate(shared, 2026, txns, i) for i in range(len(txns))]
    assert [r.values[1:4] if r else None for r in found] == [
        ("Albert Heijn 1234", "Omschrijving: boodschappen pas 12", "Debit"),
        ("Spotify AB", "Subscription", "Debit"),
        ("Albert Heijn 1234", "Omschrijving: boodschappen", "Debit"),
        ("Employer BV", "Salary January", "Credit"),
        ("A Friend", "Refund", "Credit"),
        ("A Friend", "Paid back", "Debit"),
        ("ATM 0042", "Cash", "Debit"),
    ]


def test_locate_without_balance_assertions(workdir: Path) -> None:
    rules = workdir / "bank.rules"
    rules.write_text(rules.read_text().replace("balance2", "# balance2"))
    journal = workdir / "2026" / "2026.journal"
    journal.write_text(journal.read_text().replace(" = €2,959.02", ""))
    source = load().source(None)
    txns = year_transactions(source, 2026)
    row = locate(Shared(source), 2026, txns, 3)
    assert row is not None and row.values[1] == "Employer BV"
