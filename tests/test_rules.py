import datetime as dt
import io
from decimal import Decimal
from pathlib import Path

import pytest

from hledger_review.cli import age, describe, main
from hledger_review.rules import (
    Evaluator,
    Row,
    Rule,
    RuleStats,
    compile_pattern,
    number,
    parse,
    parse_matchers,
    posting,
    posting_amount,
    posting_balance,
    read_rows,
    stats,
)

RULES = """\
# header comment
separator ;
skip 1
fields date, payee, _, notes, amount
date-format %d-%m-%Y

account1  expenses:unknown

if %payee ^AH
  account1     expenses:food
  description  Groceries

if
%notes salary
& ! %payee refund
  account1  income:work

# a comment above
if %payee Spotify && %amount ^-
  account1     expenses:subscriptions
  # an indented comment
  comment

if|account1|description
%payee shell|expenses:car|Fuel
; a table comment
%payee esso|expenses:car|Fuel

if %notes (Omschrijving): ([^:]*)
  comment  \\2
"""


# parsing
def test_rules_and_lines() -> None:
    rules = parse(RULES).rules
    assert [r.line for r in rules] == [9, 13, 19, 25, 27, 29]
    ah, salary, spotify, shell, _esso, notes = rules
    assert ah.assigns == {"account1", "description"}
    assert salary.assigns == {"account1"}
    assert len(salary.groups) == 1 and len(salary.groups[0]) == 2
    assert spotify.assigns == {"account1", "comment"}
    assert shell.assigns == {"account1", "description"}
    assert notes.assigns == {"comment"}


def test_directives() -> None:
    d = parse(RULES).directives
    assert (d.separator, d.skip, d.date_format) == (";", 1, "%d-%m-%Y")
    assert d.fields == ["date", "payee", "_", "notes", "amount"]


# matching
AH = Row(("01-01-2026", "AH Strijp", "x", "card 1", "-3.00"))
SALARY = Row(("02-01-2026", "Employer", "x", "Salary January", "100"))
REFUND = Row(("03-01-2026", "refund shop", "x", "salary refund", "5"))


@pytest.mark.parametrize(
    ("matchers", "row", "expected"),
    [
        (["%payee ^ah "], AH, True),  # case-insensitive
        (["%payee ^Strijp"], AH, False),
        (["%2 ^AH"], AH, True),  # by number
        (["strijp,x"], AH, True),  # whole record, comma-joined
        (["%nosuchfield ."], AH, False),  # unknown field: empty string
        (["! %payee ^AH"], AH, False),
        (["%notes salary", "& ! %payee refund"], SALARY, True),
        (["%notes salary", "& ! %payee refund"], REFUND, False),
        (["%notes salary && ! %payee refund"], REFUND, False),
        (["%payee nope", "%notes card"], AH, True),  # lines are OR'ed
        (["%amount ^[[:digit:]]+$"], SALARY, True),
        (["%payee \\<AH\\>"], AH, True),
    ],
)
def test_matching(matchers: list[str], row: Row, expected: bool) -> None:
    rule = Rule(1, parse_matchers(matchers), frozenset())
    assert Evaluator(parse(RULES).directives).matches(rule, row) is expected


def test_date_uses_date_format() -> None:
    assert Evaluator(parse(RULES).directives).date(AH) == dt.date(2026, 1, 1)


def test_compile_pattern_rejects_bad_regex() -> None:
    with pytest.raises(Exception, match="missing"):
        compile_pattern("(unclosed")


def test_read_rows_skips_header_and_overlap(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("h;h\n1;one\n2;two\n")
    b.write_text('h;h\n2;two\n3;"multi\nline"\n')
    rows = read_rows([a, b], parse(RULES).directives)
    assert [r.values for r in rows] == [
        ("1", "one"),
        ("2", "two"),
        ("3", "multi\nline"),
    ]


# field values
def test_assigned_follows_later_rules_and_interpolates() -> None:
    rules = parse(RULES + "\nif %payee ^AH\n  amount1  -%amount EUR\n")
    values = Evaluator(rules.directives).assigned(rules, AH)
    assert values["account1"] == "expenses:food"
    assert values["description"] == "Groceries"
    assert values["amount1"] == "--3.00 EUR"


def test_table_values() -> None:
    shell = parse(RULES).rules[3]
    assert shell.values == (("account1", "expenses:car"), ("description", "Fuel"))


@pytest.mark.parametrize(
    ("text", "mark", "expected"),
    [
        ("€-1,234.50", ".", Decimal("-1234.50")),
        ("-€1.234,50", ",", Decimal("-1234.50")),
        ("--€3,00", ",", Decimal("3.00")),
        ("€5 @ $6", ".", Decimal(5)),
        ("€", ".", None),
    ],
)
def test_number(text: str, mark: str, expected: Decimal | None) -> None:
    assert number(text, mark) == expected


def test_posting_amounts_and_balances() -> None:
    values = {"account1": "x", "account2": "bank", "amount": "5", "balance": "9"}
    assert posting(values, "bank") == 2
    assert posting(values, "nope") is None
    assert posting_amount(values, 1, ".") == 5
    assert posting_amount(values, 2, ".") == -5
    assert posting_balance(values, 1, ".") == 9
    assert posting_balance(values, 2, ".") is None
    in_out = {"amount2-in": "", "amount2-out": "7", "balance2": "1,5"}
    assert posting_amount(in_out, 2, ",") == -7
    assert posting_balance(in_out, 2, ",") == Decimal("1.5")


# statistics
def test_stats() -> None:
    text = RULES + "\nif %payee strijp\n  account1  expenses:food:groceries\n"
    rows = [AH, SALARY, REFUND]
    report = stats(parse(text), rows, "account1")
    ah, salary, spotify, _shell, _esso, notes, strijp = report.rules
    assert (ah.rows, ah.overridden, ah.overridden_by) == (1, 0, set())
    assert ah.last == dt.date(2026, 1, 1)
    assert (salary.rows, salary.last) == (1, dt.date(2026, 1, 2))
    assert (spotify.rows, notes.rows) == (0, 0)
    assert (strijp.rows, strijp.overridden) == (1, 0)
    assert report.rows == 3
    assert report.fallthrough == 1  # the refund


def test_stats_overridden_on_every_field() -> None:
    text = RULES + "\nif %payee strijp\n  account1  x\n  description  y\n"
    ah = stats(parse(text), [AH]).rules[0]
    assert (ah.overridden, ah.overridden_by) == (1, {32})


def test_stats_bad_regex() -> None:
    s = stats(parse("if %payee (unclosed\n  account1 x\n"), [AH]).rules[0]
    assert s.error is not None and s.rows == 0


def test_age() -> None:
    today = dt.date(2026, 9, 29)
    days = (0, 1, 13, 14, 59, 60, 729, 730)
    ages = [age(today - dt.timedelta(days=d), today) for d in days]
    assert ages == ["today", "1d", "13d", "2w", "8w", "2m", "24m", "2y"]
    assert age(today + dt.timedelta(days=1), today) == "today"


def test_describe() -> None:
    today = dt.date(2026, 9, 29)
    rule = Rule(1, [], frozenset({"account1"}))
    last = dt.date(2026, 9, 27)

    def text(**kw: object) -> str:
        return describe(RuleStats(rule, **kw), today)  # type: ignore[arg-type]

    assert text(rows=69, last=last) == "note: 69 matches · 2d"
    assert text() == "warning: unused"
    assert text(error="bad") == "error: bad"
    assert (
        text(rows=5, overridden=2, overridden_by={92, 88})
        == "note: 5 matches · 2 overridden by 88, 92"
    )
    assert (
        text(rows=5, overridden=5, overridden_by={1, 2, 3, 4}, last=last)
        == "warning: 5 matches · all overridden (4 rules) · 2d"
    )
    assert text(rows=4, last=dt.date(2025, 9, 29)) == "note: 4 matches · 12m"
    assert text(rows=4, last=dt.date(2025, 9, 28)) == "warning: 4 matches · 12m · stale"
    assert text(rows=1, last=last) == "warning: 1 match · 2d"


# command line
def test_cli_prints_one_line_per_rule(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = workdir / "bank.rules"
    rules.write_text(rules.read_text() + "\nif %payee Nothing\n  account1  x\n")
    (workdir / "2025").mkdir()
    (workdir / "2025" / "2025.csv").write_text("h\n20251230;Bakery;x;Debit;3,00;1\n")
    with pytest.raises(SystemExit) as exit:
        main(["rules"])
    assert exit.value.code == 0
    out, err = capsys.readouterr()

    def ago(day: int) -> str:
        return age(dt.date(2026, 1, day), dt.date.today())

    assert out.splitlines() == [
        f"{rules}:14: note: 6 matches · {ago(9)}",
        f"{rules}:18: note: 2 matches · {ago(8)}",
        f"{rules}:22: warning: 1 match · {ago(7)}",
        f"{rules}:27: warning: unused",
    ]
    assert err == "8 rows from 2 CSV files, 7 set no account1\n"


def test_cli_rules_file_from_stdin_and_explicit_csv(
    workdir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = workdir / "other.csv"
    other.write_text("h\n20260201;Employer BV;x;Credit;1,00;1,00\n")
    unsaved = (workdir / "bank.rules").read_text().replace("Employer", "Nobody")
    monkeypatch.setattr("sys.stdin", io.StringIO(unsaved))
    with pytest.raises(SystemExit):
        main(["rules", "--rules", "bank.rules", "--stdin", str(other)])
    out, err = capsys.readouterr()
    ago = age(dt.date(2026, 2, 1), dt.date.today())
    assert out.splitlines()[1:] == [
        f"bank.rules:18: warning: 1 match · {ago}",
        "bank.rules:22: warning: unused",
    ]
    assert err == "1 row from 1 CSV file, 1 set no account1\n"


def test_cli_rules_without_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "x.csv").write_text("a,b\n")
    (tmp_path / "x.csv.rules").write_text("fields a, b\nif %a a\n  account1  y\n")
    with pytest.raises(SystemExit):
        main(["rules", "-r", "x.csv.rules"])
    out, err = capsys.readouterr()
    assert out == "x.csv.rules:2: warning: 1 match\n"
    assert err == "1 row from 1 CSV file\n"


def test_cli_missing_csv(workdir: Path) -> None:
    with pytest.raises(SystemExit, match=r"no such CSV file: nope\.csv"):
        main(["rules", "nope.csv"])
