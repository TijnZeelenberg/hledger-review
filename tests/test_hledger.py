from pathlib import Path

import pytest

from hledger_review.hledger import (
    append_rule,
    one_off_matcher,
    rules_pattern,
    set_one_off,
)


def test_rules_pattern_escapes_metacharacters() -> None:
    assert rules_pattern("McDonald's (A1) 1.5$") == r"McDonald's \(A1\) 1\.5\$"


def test_append_rule(tmp_path: Path) -> None:
    rules = tmp_path / "x.rules"
    rules.write_text("skip 1\n")
    append_rule(rules, "payee", "^AH ", "expenses:food", "Groceries", "account1")
    assert rules.read_text() == (
        "skip 1\n"
        "\nif %payee ^AH \n"
        "  account1     expenses:food\n"
        "  description  Groceries\n"
        "  comment\n"
    )


# one-offs
MATCHER = "%date ^20260105$ && %saldo ^964,02$"


def test_one_off_matcher_escapes_values() -> None:
    key = [("date", "20260105"), ("amount", "1.00"), ("notes", "")]
    assert one_off_matcher(key) == r"%date ^20260105$ && %amount ^1\.00$ && %notes ^$"


def test_set_one_off_starts_a_table(tmp_path: Path) -> None:
    rules = tmp_path / "one-offs.rules"
    rules.write_text("# 2026\n\ninclude ../bank.rules\n")
    set_one_off(rules, MATCHER, {"account1": "expenses:x", "description": "X"})
    assert rules.read_text() == (
        "# 2026\n\ninclude ../bank.rules\n\n"
        f"if|account1|description\n{MATCHER}|expenses:x|X\n"
    )


def test_set_one_off_adds_to_and_replaces_in_the_table(tmp_path: Path) -> None:
    rules = tmp_path / "one-offs.rules"
    rules.write_text(
        "include x.rules\n\nif|description|account1|comment\n"
        "%date ^1$|A|a|\n\nif %payee y\n  comment  z\n"
    )
    values = {"account1": "b", "description": "B", "comment": ""}
    set_one_off(rules, MATCHER, values)
    set_one_off(rules, "%date ^1$", values | {"account1": "c"})
    assert rules.read_text() == (
        "include x.rules\n\nif|description|account1|comment\n"
        f"%date ^1$|B|c|\n{MATCHER}|B|b|\n\nif %payee y\n  comment  z\n"
    )


def test_set_one_off_refuses_unknown_columns(tmp_path: Path) -> None:
    rules = tmp_path / "one-offs.rules"
    rules.write_text("if|account1|amount1\n%date ^1$|a|1\n")
    with pytest.raises(ValueError, match="cannot fill column amount1"):
        set_one_off(rules, MATCHER, {"account1": "b", "description": "B"})
