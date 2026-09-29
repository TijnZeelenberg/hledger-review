from pathlib import Path

from hledger_review import completion
from hledger_review.completion import candidates, rank, tag_token
from hledger_review.config import load
from hledger_review.journal import transactions
from tests.conftest import needs_hledger

JOURNAL = """\
2026-01-01 Groceries  ; reis:gent
    expenses:food  €1
    assets

2026-01-02 Groceries  ; Omschrijving: bank text, reis:praag
    ; vast:
    expenses:food  €1  ; type: X
    assets

2026-01-03 Train  ; reis:gent, _hidden:x
    expenses:travel  €1
    assets

2026-01-04 ALBERT HEIJN 1234
    expenses:unknown  €1
    assets
"""


def test_candidates_rank_by_frequency() -> None:
    c = candidates(transactions(JOURNAL), "expenses:unknown")
    assert c.descriptions == ["Groceries", "Train"]
    assert c.tags == ["reis", "reis:gent", "reis:praag", "vast"]


def test_rank_puts_prefix_matches_first() -> None:
    pool = ["Bier in Gent", "Groceries", "Gent trip", "gent"]
    assert rank("gent", pool) == [("Gent trip", 0), ("Bier in Gent", 8)]
    assert rank("", pool) == []


def test_tag_token() -> None:
    assert tag_token("vast, reis:g", 12) == (6, "reis:g")
    assert tag_token("re, vast", 2) == (0, "re")
    assert tag_token("vast,  ", 7) == (7, "")


@needs_hledger
def test_load_reads_the_journal(history_workdir: Path) -> None:
    config = load()
    c = completion.load(config.journal, config.unmarked)
    assert c.descriptions[0] == "Groceries"
    assert "Albert Heijn 1234" not in c.descriptions  # still unmarked
    assert {"reis:gent", "reis:praag", "vast"} <= set(c.tags)
    assert not any(t.startswith("Omschrijving") for t in c.tags)
