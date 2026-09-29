from pathlib import Path

from hledger_review.hledger import append_rule, rules_pattern


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
